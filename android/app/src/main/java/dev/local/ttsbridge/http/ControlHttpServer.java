package dev.local.ttsbridge.http;

import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * A deliberately tiny HTTP server - no external library, so the app stays a
 * single APK with no build-time dependency resolution. Good enough for a
 * handful of local control requests from Home Assistant; not meant to face
 * the open internet.
 *
 * Routes are registered as (METHOD + " " + path) -> Handler.
 */
public class ControlHttpServer {

    public interface Handler {
        /** @return {statusCode, jsonBody} */
        Response handle(JSONObject body, Map<String, String> queryParams) throws JSONException;
    }

    public static class Response {
        final int status;
        final JSONObject json;
        public Response(int status, JSONObject json) { this.status = status; this.json = json; }
    }

    private static final String TAG = "TtsBridge/Http";

    private final int port;
    private final Map<String, Handler> routes = new HashMap<>();
    private ServerSocket serverSocket;
    private ExecutorService pool;
    private volatile boolean running = false;

    public ControlHttpServer(int port) {
        this.port = port;
    }

    public void route(String method, String path, Handler handler) {
        routes.put(method.toUpperCase() + " " + path, handler);
    }

    public void start() {
        if (running) return;
        running = true;
        pool = Executors.newCachedThreadPool();
        pool.execute(this::acceptLoop);
    }

    public void stop() {
        running = false;
        try {
            if (serverSocket != null) serverSocket.close();
        } catch (IOException ignored) {
        }
        if (pool != null) pool.shutdownNow();
    }

    private void acceptLoop() {
        try {
            serverSocket = new ServerSocket(port);
            Log.i(TAG, "Listening on :" + port);
            while (running) {
                Socket socket = serverSocket.accept();
                pool.execute(() -> handleConnection(socket));
            }
        } catch (IOException e) {
            if (running) Log.e(TAG, "Server socket error", e);
        }
    }

    private void handleConnection(Socket socket) {
        try {
            socket.setSoTimeout(10000);
            java.io.InputStream in = socket.getInputStream();

            String requestLine = readHeaderLine(in);
            if (requestLine == null || requestLine.isEmpty()) return;

            String[] parts = requestLine.split(" ");
            if (parts.length < 2) { writeRaw(socket, 400, "{\"error\":\"bad_request\"}"); return; }
            String method = parts[0];
            String rawPath = parts[1];
            String path = rawPath.contains("?") ? rawPath.substring(0, rawPath.indexOf('?')) : rawPath;
            Map<String, String> query = parseQuery(rawPath.contains("?") ? rawPath.substring(rawPath.indexOf('?') + 1) : "");

            int contentLength = 0;
            String line;
            while ((line = readHeaderLine(in)) != null && !line.isEmpty()) {
                if (line.toLowerCase().startsWith("content-length:")) {
                    contentLength = Integer.parseInt(line.substring(line.indexOf(':') + 1).trim());
                }
            }

            // Content-Length is a BYTE count per the HTTP spec. Read exactly
            // that many raw bytes - NOT characters - and decode as UTF-8
            // only once every byte is in hand. Reading through a
            // char-decoding Reader here (the previous approach) breaks for
            // any multi-byte UTF-8 character: e.g. an em dash "-" is 3
            // bytes but 1 char, so a char-counted read loop ends up
            // expecting more characters than the decoded body actually
            // contains and blocks forever waiting for bytes the client
            // already finished sending - which is exactly what was
            // surfacing as a SocketTimeoutException here.
            byte[] bodyBytes = new byte[contentLength];
            int read = 0;
            while (read < contentLength) {
                int n = in.read(bodyBytes, read, contentLength - read);
                if (n < 0) break;
                read += n;
            }
            String bodyStr = new String(bodyBytes, 0, read, StandardCharsets.UTF_8);

            JSONObject body;
            try {
                body = bodyStr.trim().isEmpty() ? new JSONObject() : new JSONObject(bodyStr);
            } catch (Exception e) {
                writeRaw(socket, 400, "{\"error\":\"invalid_json\"}");
                return;
            }

            Handler handler = routes.get(method.toUpperCase() + " " + path);
            if (handler == null) {
                writeRaw(socket, 404, "{\"error\":\"not_found\"}");
                return;
            }

            Response resp;
            try {
                resp = handler.handle(body, query);
            } catch (Exception e) {
                Log.e(TAG, "Handler error", e);
                resp = new Response(500, errorJson(String.valueOf(e.getMessage())));
            }
            writeRaw(socket, resp.status, resp.json.toString());
        } catch (Exception e) {
            // Anything unexpected here previously fell through uncaught,
            // leaving the socket to close with zero bytes written - that's
            // exactly a curl "(52) Empty reply from server". Catch
            // everything so we always answer instead of going silent.
            Log.e(TAG, "Connection error", e);
            try {
                writeRaw(socket, 500, errorJson("internal_error: " + e).toString());
            } catch (IOException ignored) {
                // socket is already toast, nothing more we can do
            }
        } finally {
            try {
                socket.close();
            } catch (IOException ignored) {
            }
        }
    }

    /** Reads one HTTP header/request-line, byte by byte, up to and excluding
     *  the terminating CRLF (bare LF also accepted). Decoded as ISO-8859-1 -
     *  a direct 1:1 byte<->char mapping with no multi-byte decoding at all,
     *  which is what header/request-line syntax actually is per the HTTP
     *  spec (ASCII-only). Deliberately NOT UTF-8 here: that's for the body
     *  only, once we know its exact byte length from Content-Length. */
    private String readHeaderLine(java.io.InputStream in) throws IOException {
        java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
        int b;
        boolean any = false;
        while ((b = in.read()) != -1) {
            any = true;
            if (b == '\n') break;
            if (b != '\r') buf.write(b);
        }
        if (!any && buf.size() == 0) return null;
        return buf.toString("ISO-8859-1");
    }

    private Map<String, String> parseQuery(String qs) {
        Map<String, String> map = new HashMap<>();
        for (String pair : qs.split("&")) {
            if (pair.isEmpty()) continue;
            int eq = pair.indexOf('=');
            if (eq < 0) map.put(pair, "");
            else map.put(pair.substring(0, eq), pair.substring(eq + 1));
        }
        return map;
    }

    private void writeRaw(Socket s, int status, String jsonBody) throws IOException {
        byte[] bodyBytes = jsonBody.getBytes(StandardCharsets.UTF_8);
        String statusText = status == 200 ? "OK" : status == 202 ? "Accepted"
                : status == 400 ? "Bad Request" : status == 404 ? "Not Found" : status == 500 ? "Internal Server Error" : "Status";
        OutputStream out = s.getOutputStream();
        String headers = "HTTP/1.1 " + status + " " + statusText + "\r\n"
                + "Content-Type: application/json\r\n"
                + "Content-Length: " + bodyBytes.length + "\r\n"
                + "Connection: close\r\n\r\n";
        out.write(headers.getBytes(StandardCharsets.UTF_8));
        out.write(bodyBytes);
        out.flush();
    }

    public static JSONObject errorJson(String message) {
        try {
            return new JSONObject().put("error", message);
        } catch (Exception e) {
            return new JSONObject();
        }
    }
}