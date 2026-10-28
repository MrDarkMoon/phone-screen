package app.phonescreen;

import android.app.Activity;
import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Rect;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.hardware.SensorManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

import android.content.pm.PackageManager;
import android.graphics.ImageFormat;
import android.hardware.camera2.CameraCaptureSession;
import android.hardware.camera2.CameraCharacteristics;
import android.hardware.camera2.CameraDevice;
import android.hardware.camera2.CameraManager;
import android.hardware.camera2.CaptureRequest;
import android.hardware.camera2.params.OutputConfiguration;
import android.hardware.camera2.params.SessionConfiguration;
import android.media.Image;
import android.media.ImageReader;
import android.os.HandlerThread;
import android.util.Base64;
import java.nio.ByteBuffer;

/**
 * Phone Screen's phone side. One button: Start tracking. Then the screen shows the
 * tracking pattern the headset's cameras find, and the app sends the Frame its motion
 * sensors and every touch, over adb (adb reverse tcp:8778 brings 127.0.0.1:8778 to the Frame).
 *
 * Lines it sends:  hello W H XDPI YDPI | train 0/1 | stopped | r T x y z w | a T x y z | p T cm | t ACTION ID X Y P | pong N T
 * Lines it reads:  level N (0-255 pattern brightness) | ping N | start | stop | snap N
 * snap N: N pictures from the front (selfie) camera, sent back as  jpg T BASE64  lines
 * (T the camera's own time stamp), or  snaperr WHY.
 * cam on|off: a steady stream of small selfie pictures while tracking (for face tracking):
 *   caminfo W H FX FY CX CY SENSOR_ORIENTATION TIMESTAMP_SOURCE, then  cam T BASE64_JPEG  lines.
 * T is the sensor's own time (elapsedRealtimeNanos); ping/pong lets the Frame match clocks.
 */
public class MainActivity extends Activity implements SensorEventListener {
    static final int PORT = 8778;
    static final float MARKER_MM = 56f;
    static final float[] MARKER_CY_MM = {36f, -36f}; // above / below the centre (y up)
    // ArUco DICT_4X4_50 ids 0 and 1: 6x6 cells, border included, row by row, 1 = white
    static final String[] MARKERS = {
        "000000010110001010000110000100000000",
        "000000000000011110010010010100000000"};

    final Handler ui = new Handler(Looper.getMainLooper());
    final LinkedBlockingQueue<String> out = new LinkedBlockingQueue<>(2000);
    volatile boolean connected = false;
    volatile boolean tracking = false;
    SensorManager sm;
    PatternView pattern;
    LinearLayout startScreen;
    TextView status;
    final OnBackInvokedCallback ignoreBack = () -> { };     // back gestures are for the VR screen

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        sm = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
        FrameLayout root = new FrameLayout(this);
        pattern = new PatternView(this);
        pattern.setVisibility(View.GONE);
        root.addView(pattern);

        startScreen = new LinearLayout(this);
        startScreen.setOrientation(LinearLayout.VERTICAL);
        startScreen.setGravity(Gravity.CENTER);
        startScreen.setBackgroundColor(Color.rgb(24, 28, 36));
        TextView title = new TextView(this);
        title.setText("Phone Screen");
        title.setTextSize(30);
        title.setTextColor(Color.WHITE);
        title.setGravity(Gravity.CENTER);
        Button go = new Button(this);
        go.setText("Start tracking");
        go.setTextSize(28);
        go.setAllCaps(false);
        go.setPadding(60, 40, 60, 40);
        go.setOnClickListener(v -> start(false));
        Button teach = new Button(this);
        teach.setText("Training");
        teach.setTextSize(24);
        teach.setAllCaps(false);
        teach.setPadding(60, 30, 60, 30);
        teach.setOnClickListener(v -> start(true));
        status = new TextView(this);
        status.setTextSize(20);
        status.setTextColor(Color.LTGRAY);
        status.setGravity(Gravity.CENTER);
        status.setPadding(40, 50, 40, 0);
        TextView help = new TextView(this);
        help.setText("To stop: hold three fingers on the screen for two seconds.\n\n"
                     + "Training: line the phone up with the see-through copy in VR, then tap.");
        help.setTextSize(18);
        help.setTextColor(Color.GRAY);
        help.setGravity(Gravity.CENTER);
        help.setPadding(60, 60, 60, 0);
        startScreen.addView(title);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, -2);
        lp.topMargin = 80;
        startScreen.addView(go, lp);
        LinearLayout.LayoutParams lp2 = new LinearLayout.LayoutParams(-2, -2);
        lp2.topMargin = 40;
        startScreen.addView(teach, lp2);
        startScreen.addView(status);
        startScreen.addView(help);
        root.addView(startScreen);
        setContentView(root);
        showStatus();

        new Thread(this::linkLoop, "link").start();
    }

    void showStatus() {
        ui.post(() -> status.setText(connected ? "Connected to the Frame" : "Waiting for the Frame…"));
    }

    volatile boolean training = false;

    void start(boolean train) {
        if (tracking) return;
        tracking = true;
        training = train;
        startScreen.setVisibility(View.GONE);
        pattern.setVisibility(View.VISIBLE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        WindowInsetsController c = getWindow().getInsetsController();
        if (c != null) {
            c.hide(WindowInsets.Type.systemBars());
            c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
        }
        getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
            OnBackInvokedDispatcher.PRIORITY_DEFAULT, ignoreBack);
        Sensor rot = sm.getDefaultSensor(Sensor.TYPE_GAME_ROTATION_VECTOR);
        Sensor acc = sm.getDefaultSensor(Sensor.TYPE_LINEAR_ACCELERATION);
        Sensor prox = sm.getDefaultSensor(Sensor.TYPE_PROXIMITY);    // keeps its infrared light on
        if (rot != null) sm.registerListener(this, rot, 5000);
        if (acc != null) sm.registerListener(this, acc, 5000);
        if (prox != null) sm.registerListener(this, prox, SensorManager.SENSOR_DELAY_FASTEST);
        sendHello();
    }

    void stop() {
        if (!tracking) return;
        tracking = false;
        sm.unregisterListener(this);
        getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(ignoreBack);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        WindowInsetsController c = getWindow().getInsetsController();
        if (c != null) c.show(WindowInsets.Type.systemBars());
        pattern.setVisibility(View.GONE);
        startScreen.setVisibility(View.VISIBLE);
        stopStream();
        send("stopped");
    }

    @Override
    protected void onPause() {
        super.onPause();
        stop();         // screen off or another app came up: nothing to track
    }

    void sendHello() {
        if (pattern.getWidth() > 0) {
            android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
            send("hello " + pattern.getWidth() + " " + pattern.getHeight() + " " + dm.xdpi + " " + dm.ydpi);
            send("train " + (training ? 1 : 0));
        }
        else ui.postDelayed(this::sendHello, 100);
    }

    void send(String line) {
        if (connected) out.offer(line);
    }

    // ---- sensors

    @Override
    public void onSensorChanged(SensorEvent e) {
        float[] v = e.values;
        switch (e.sensor.getType()) {
            case Sensor.TYPE_GAME_ROTATION_VECTOR:
                send("r " + e.timestamp + " " + v[0] + " " + v[1] + " " + v[2] + " " + (v.length > 3 ? v[3] : -1));
                break;
            case Sensor.TYPE_LINEAR_ACCELERATION:
                send("a " + e.timestamp + " " + v[0] + " " + v[1] + " " + v[2]);
                break;
            case Sensor.TYPE_PROXIMITY:
                send("p " + e.timestamp + " " + v[0]);
                break;
        }
    }

    @Override
    public void onAccuracyChanged(Sensor s, int a) { }

    // ---- the link to the Frame

    void linkLoop() {
        while (true) {
            try (Socket s = new Socket()) {
                s.connect(new InetSocketAddress("127.0.0.1", PORT), 2000);
                s.setTcpNoDelay(true);
                out.clear();
                connected = true;
                showStatus();
                if (tracking) sendHello();
                Thread reader = new Thread(() -> readLoop(s), "link-read");
                reader.start();
                OutputStream os = s.getOutputStream();
                List<String> batch = new ArrayList<>();
                while (!s.isClosed()) {
                    String first = out.poll(500, TimeUnit.MILLISECONDS);
                    if (first == null) continue;
                    batch.clear();
                    batch.add(first);
                    out.drainTo(batch);
                    StringBuilder sb = new StringBuilder();
                    for (String l : batch) sb.append(l).append('\n');
                    os.write(sb.toString().getBytes(StandardCharsets.UTF_8));
                    os.flush();
                }
            } catch (Exception e) {
                // the Frame's side isn't running, or adb reverse isn't set up yet
            }
            if (connected) {
                connected = false;
                showStatus();
            }
            SystemClock.sleep(1000);
        }
    }

    void readLoop(Socket s) {
        try {
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            String line;
            while ((line = in.readLine()) != null) {
                String[] w = line.trim().split(" ");
                if (w[0].equals("ping") && w.length > 1) {
                    send("pong " + w[1] + " " + SystemClock.elapsedRealtimeNanos());
                } else if (w[0].equals("level") && w.length > 1) {
                    int lv = Integer.parseInt(w[1]);
                    ui.post(() -> pattern.setLevel(lv));
                } else if (w[0].equals("start")) {
                    ui.post(() -> start(false));
                } else if (w[0].equals("cam") && w.length > 1) {
                    boolean on = w[1].equals("on");
                    ui.post(() -> { if (on) startStream(); else stopStream(); });
                } else if (w[0].equals("snap")) {
                    int n = w.length > 1 ? Integer.parseInt(w[1]) : 1;
                    ui.post(() -> snap(n));
                } else if (w[0].equals("stop")) {
                    ui.post(this::stop);
                }
            }
        } catch (Exception e) {
            // closed
        }
        try { s.close(); } catch (Exception e) { }
    }

    // ---- the front camera, on request: a few pictures for the Frame to look at

    HandlerThread camThread;
    boolean snapping = false;

    void snap(int count) {
        if (checkSelfPermission(android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            send("snaperr asking for camera permission");
            requestPermissions(new String[]{android.Manifest.permission.CAMERA}, 1);
            return;
        }
        if (snapping) return;
        try {
            CameraManager cm = getSystemService(CameraManager.class);
            String id = null;
            for (String c : cm.getCameraIdList()) {
                Integer f = cm.getCameraCharacteristics(c).get(CameraCharacteristics.LENS_FACING);
                if (f != null && f == CameraCharacteristics.LENS_FACING_FRONT) { id = c; break; }
            }
            if (id == null) { send("snaperr no front camera"); return; }
            if (camThread == null) { camThread = new HandlerThread("camera"); camThread.start(); }
            Handler h = new Handler(camThread.getLooper());
            ImageReader reader = ImageReader.newInstance(1280, 960, ImageFormat.JPEG, 4);
            final int[] seen = {0}, sent = {0};
            final CameraDevice[] dev = {null};
            snapping = true;
            reader.setOnImageAvailableListener(r -> {
                Image img = r.acquireNextImage();
                if (img == null) return;
                seen[0]++;
                // let the exposure settle, then take every other frame (the headset's lights flash)
                if (seen[0] > 20 && sent[0] < count && seen[0] % 2 == 0) {
                    ByteBuffer b = img.getPlanes()[0].getBuffer();
                    byte[] a = new byte[b.remaining()];
                    b.get(a);
                    send("jpg " + img.getTimestamp() + " " + Base64.encodeToString(a, Base64.NO_WRAP));
                    sent[0]++;
                }
                img.close();
                if (sent[0] >= count && dev[0] != null) {
                    dev[0].close();
                    dev[0] = null;
                    snapping = false;
                }
            }, h);
            cm.openCamera(id, new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice d) {
                    dev[0] = d;
                    try {
                        d.createCaptureSession(new SessionConfiguration(SessionConfiguration.SESSION_REGULAR,
                            List.of(new OutputConfiguration(reader.getSurface())), h::post,
                            new CameraCaptureSession.StateCallback() {
                                @Override public void onConfigured(CameraCaptureSession cs) {
                                    try {
                                        CaptureRequest.Builder rb = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                                        rb.addTarget(reader.getSurface());
                                        cs.setRepeatingRequest(rb.build(), null, h);
                                    } catch (Exception e) { send("snaperr " + e); snapping = false; }
                                }
                                @Override public void onConfigureFailed(CameraCaptureSession cs) {
                                    send("snaperr camera setup failed");
                                    snapping = false;
                                }
                            }));
                    } catch (Exception e) { send("snaperr " + e); snapping = false; }
                }
                @Override public void onDisconnected(CameraDevice d) { d.close(); snapping = false; }
                @Override public void onError(CameraDevice d, int e) { send("snaperr camera error " + e); d.close(); snapping = false; }
            }, h);
        } catch (Exception e) {
            send("snaperr " + e);
            snapping = false;
        }
    }

    // ---- a steady stream of small selfie pictures (cam on), for the Frame's face tracking

    static final int CAM_W = 640, CAM_H = 480;
    static final long CAM_EVERY_NS = 66_000_000;        // about 15 a second
    CameraDevice streamDev;
    ImageReader streamReader;
    long lastSent = 0;

    void startStream() {
        if (streamDev != null || !tracking) return;
        if (checkSelfPermission(android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            send("snaperr asking for camera permission");
            requestPermissions(new String[]{android.Manifest.permission.CAMERA}, 1);
            return;
        }
        try {
            CameraManager cm = getSystemService(CameraManager.class);
            String id = null;
            CameraCharacteristics ch = null;
            for (String c : cm.getCameraIdList()) {
                CameraCharacteristics cc = cm.getCameraCharacteristics(c);
                Integer f = cc.get(CameraCharacteristics.LENS_FACING);
                if (f != null && f == CameraCharacteristics.LENS_FACING_FRONT) { id = c; ch = cc; break; }
            }
            if (id == null) { send("snaperr no front camera"); return; }
            sendCamInfo(ch);
            if (camThread == null) { camThread = new HandlerThread("camera"); camThread.start(); }
            Handler h = new Handler(camThread.getLooper());
            streamReader = ImageReader.newInstance(CAM_W, CAM_H, ImageFormat.YUV_420_888, 3);
            streamReader.setOnImageAvailableListener(r -> {
                Image img = r.acquireLatestImage();
                if (img == null) return;
                if (img.getTimestamp() - lastSent >= CAM_EVERY_NS && connected) {
                    lastSent = img.getTimestamp();
                    send("cam " + img.getTimestamp() + " " + Base64.encodeToString(toJpeg(img), Base64.NO_WRAP));
                }
                img.close();
            }, h);
            final ImageReader reader = streamReader;
            cm.openCamera(id, new CameraDevice.StateCallback() {
                @Override public void onOpened(CameraDevice d) {
                    streamDev = d;
                    try {
                        d.createCaptureSession(new SessionConfiguration(SessionConfiguration.SESSION_REGULAR,
                            List.of(new OutputConfiguration(reader.getSurface())), h::post,
                            new CameraCaptureSession.StateCallback() {
                                @Override public void onConfigured(CameraCaptureSession cs) {
                                    try {
                                        CaptureRequest.Builder rb = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW);
                                        rb.addTarget(reader.getSurface());
                                        cs.setRepeatingRequest(rb.build(), null, h);
                                    } catch (Exception e) { send("snaperr " + e); }
                                }
                                @Override public void onConfigureFailed(CameraCaptureSession cs) { send("snaperr stream setup failed"); }
                            }));
                    } catch (Exception e) { send("snaperr " + e); }
                }
                @Override public void onDisconnected(CameraDevice d) { d.close(); streamDev = null; }
                @Override public void onError(CameraDevice d, int e) { send("snaperr stream error " + e); d.close(); streamDev = null; }
            }, h);
        } catch (Exception e) {
            send("snaperr " + e);
        }
    }

    void stopStream() {
        if (streamDev != null) { streamDev.close(); streamDev = null; }
        if (streamReader != null) { streamReader.close(); streamReader = null; }
    }

    /** The lens's focal length and centre in the stream's pixels, so the Frame can turn a pixel into a direction. */
    void sendCamInfo(CameraCharacteristics ch) {
        float fx = 0, fy = 0, cx = CAM_W / 2f, cy = CAM_H / 2f;
        float[] k = ch.get(CameraCharacteristics.LENS_INTRINSIC_CALIBRATION);
        android.graphics.Rect act = ch.get(CameraCharacteristics.SENSOR_INFO_PRE_CORRECTION_ACTIVE_ARRAY_SIZE);
        if (act != null) {
            // the stream is the middle of the sensor, cut to 4:3 and scaled down
            float cw = Math.min(act.width(), act.height() * (float) CAM_W / CAM_H);
            float chh = cw * CAM_H / CAM_W;
            float x0 = (act.width() - cw) / 2f, y0 = (act.height() - chh) / 2f, sc = CAM_W / cw;
            if (k != null && k[0] > 0) {
                fx = k[0] * sc; fy = k[1] * sc; cx = (k[2] - x0) * sc; cy = (k[3] - y0) * sc;
            } else {
                float[] fl = ch.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS);
                android.util.SizeF phys = ch.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE);
                if (fl != null && phys != null) {
                    fx = fy = fl[0] / phys.getWidth() * act.width() * sc;
                }
            }
        }
        Integer orient = ch.get(CameraCharacteristics.SENSOR_ORIENTATION);
        Integer tsrc = ch.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE);
        send("caminfo " + CAM_W + " " + CAM_H + " " + fx + " " + fy + " " + cx + " " + cy + " "
             + (orient == null ? -1 : orient) + " " + (tsrc == null ? -1 : tsrc));
    }

    static byte[] toJpeg(Image img) {
        int w = img.getWidth(), h = img.getHeight();
        byte[] nv21 = new byte[w * h * 3 / 2];
        Image.Plane[] p = img.getPlanes();
        ByteBuffer y = p[0].getBuffer(), u = p[1].getBuffer(), v = p[2].getBuffer();
        int yr = p[0].getRowStride(), ur = p[1].getRowStride(), up = p[1].getPixelStride();
        for (int r = 0; r < h; r++) {
            y.position(r * yr);
            y.get(nv21, r * w, w);
        }
        int o = w * h;
        for (int r = 0; r < h / 2; r++)
            for (int c = 0; c < w / 2; c++) {
                int i = r * ur + c * up;
                nv21[o++] = v.get(i);
                nv21[o++] = u.get(i);
            }
        android.graphics.YuvImage yi = new android.graphics.YuvImage(nv21, ImageFormat.NV21, w, h, null);
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        yi.compressToJpeg(new android.graphics.Rect(0, 0, w, h), 70, out);
        return out.toByteArray();
    }

    // ---- the pattern, and the touches on it

    class PatternView extends View {
        final Paint white = new Paint(), black = new Paint(), dim = new Paint();
        int level = 210;
        long threeSince = 0;

        PatternView(Context c) {
            super(c);
            white.setColor(Color.WHITE);
            black.setColor(Color.BLACK);
        }

        void setLevel(int lv) {
            level = Math.max(0, Math.min(255, lv));
            invalidate();
        }

        @Override
        protected void onLayout(boolean changed, int l, int t, int r, int b) {
            super.onLayout(changed, l, t, r, b);
            // keep the side edges for the VR screen's back swipe, not Android's
            int h = getHeight(), w = getWidth(), band = (int) (200 * getResources().getDisplayMetrics().density);
            List<Rect> ex = new ArrayList<>();
            ex.add(new Rect(0, h / 2 - band / 2, 60, h / 2 + band / 2));
            ex.add(new Rect(w - 60, h / 2 - band / 2, w, h / 2 + band / 2));
            setSystemGestureExclusionRects(ex);
        }

        @Override
        protected void onDraw(Canvas g) {
            int w = getWidth(), h = getHeight();
            g.drawRect(0, 0, w, h, white);
            // true millimetres: the panel's own pixel density, across and down
            android.util.DisplayMetrics dm = getResources().getDisplayMetrics();
            float mmX = dm.xdpi / 25.4f, mmY = dm.ydpi / 25.4f;
            float cw = MARKER_MM * mmX / 6f, ch = MARKER_MM * mmY / 6f;
            for (int k = 0; k < 2; k++) {
                float x0 = w / 2f - 3 * cw, y0 = h / 2f - MARKER_CY_MM[k] * mmY - 3 * ch;
                String bits = MARKERS[k];
                for (int i = 0; i < 36; i++) {
                    if (bits.charAt(i) == '1') continue;
                    float x = x0 + (i % 6) * cw, y = y0 + (i / 6) * ch;
                    // overlap by a pixel so no white seams show between black cells
                    g.drawRect(x, y, x + cw + 1, y + ch + 1, black);
                }
            }
            dim.setColor(Color.argb(255 - level, 0, 0, 0));      // the cameras' view blooms: dim it all
            g.drawRect(0, 0, w, h, dim);
        }

        @Override
        public boolean onTouchEvent(MotionEvent e) {
            int a = e.getActionMasked();
            String act;
            switch (a) {
                case MotionEvent.ACTION_DOWN: case MotionEvent.ACTION_POINTER_DOWN: act = "down"; break;
                case MotionEvent.ACTION_UP: case MotionEvent.ACTION_POINTER_UP:
                case MotionEvent.ACTION_CANCEL: act = "up"; break;
                case MotionEvent.ACTION_MOVE: act = "move"; break;
                default: return true;
            }
            float w = getWidth(), h = getHeight();
            if (act.equals("move") || a == MotionEvent.ACTION_CANCEL) {
                for (int i = 0; i < e.getPointerCount(); i++) sendTouch(act, e, i, w, h);
            } else {
                sendTouch(act, e, e.getActionIndex(), w, h);
            }
            // three fingers held still for two seconds: stop
            if (e.getPointerCount() >= 3 && a != MotionEvent.ACTION_UP && a != MotionEvent.ACTION_CANCEL) {
                if (threeSince == 0) threeSince = SystemClock.uptimeMillis();
                else if (SystemClock.uptimeMillis() - threeSince > 2000) { threeSince = 0; stop(); }
            } else {
                threeSince = 0;
            }
            return true;
        }

        void sendTouch(String act, MotionEvent e, int i, float w, float h) {
            send("t " + act + " " + e.getPointerId(i) + " " + (e.getX(i) / w) + " " + (e.getY(i) / h)
                 + " " + Math.max(0.05f, Math.min(1f, e.getPressure(i))));
        }
    }
}
