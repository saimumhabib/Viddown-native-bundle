package viddown.newpipe;

import com.grack.nanojson.JsonParser;
import org.jsoup.Jsoup;
import org.schabi.newpipe.extractor.MediaFormat;
import org.schabi.newpipe.extractor.NewPipe;
import org.schabi.newpipe.extractor.ServiceList;
import org.schabi.newpipe.extractor.StreamingService;
import org.schabi.newpipe.extractor.downloader.Request;
import org.schabi.newpipe.extractor.downloader.Response;
import org.schabi.newpipe.extractor.stream.Description;
import org.schabi.newpipe.extractor.utils.JavaScript;

import javax.net.ssl.SSLContext;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.schabi.newpipe.extractor.services.youtube.protos.playlist.PlaylistProtobufContinuation.ContinuationParams;
import static org.schabi.newpipe.extractor.services.youtube.protos.playlist.PlaylistProtobufContinuation.PlaylistContinuation;

/**
 * "selftest": exercises, without any site's cooperation, the parts of the extractor that break in a
 * GraalVM native build (they rely on reflection or on resource files), and does ONE HTTPS request
 * to prove the TLS stack and the CA bundle work.
 *
 * Two of the offline checks cover what every real request needs first: that java.net.URL knows the
 * https protocol, and that the extractor recognises YouTube addresses (NewPipe.getServiceByUrl).
 * Without them a build can pass every other check and still refuse every address it is given.
 *
 * The same checks run twice in CI: on the JVM under the native-image tracing agent (which records
 * what they touch) and in the finished native executable (which must then behave the same).
 */
final class SelfTest {

    private static final String DEFAULT_URL = "https://github.com/robots.txt";

    private interface Check {
        String run() throws Exception;
    }

    private SelfTest() { }

    @SuppressWarnings("deprecation")
    static Map<String, Object> run(String[] args) {
        String cacert = null;
        String url = DEFAULT_URL;
        boolean offline = false;
        for (int i = 1; i < args.length; i++) {
            switch (args[i]) {
                case "--cacert": cacert = i + 1 < args.length ? args[++i] : null; break;
                case "--url": url = i + 1 < args.length ? args[++i] : url; break;
                case "--offline": offline = true; break;
                default: break;
            }
        }
        final String caArg = cacert;
        final String target = url;

        Map<String, Object> checks = new LinkedHashMap<>();
        boolean ok = true;
        ok &= check(checks, "javascript", () -> {
            // the way the extractor runs YouTube's signature code (Rhino, interpreted mode)
            String r = JavaScript.run("function f(a) { return a + '!'; }", "f", "hi");
            require("hi!".equals(r), "got " + r);
            return r;
        });
        ok &= check(checks, "html", () -> {
            String t = Jsoup.parse("<p>a &amp; b &eacute;</p>").text();   // needs jsoup's entity tables
            require("a & b \u00e9".equals(t), "got " + t);
            return t;
        });
        ok &= check(checks, "json", () -> {
            int n = JsonParser.object().from("{\"n\":[1,2,3]}").getArray("n").size();
            require(n == 3, "got " + n);
            return "3 items";
        });
        ok &= check(checks, "protobuf", () -> {
            PlaylistContinuation msg = PlaylistContinuation.newBuilder()
                    .setParameters(ContinuationParams.newBuilder().setBrowseId("VLPLtest").setPlaylistId("PLtest").build())
                    .build();
            byte[] bytes = msg.toByteArray();
            PlaylistContinuation back = PlaylistContinuation.parseFrom(bytes);
            require(bytes.length > 0 && "PLtest".equals(back.getParameters().getPlaylistId()), "round trip failed");
            return bytes.length + " bytes";
        });
        ok &= check(checks, "reflection", () -> {
            // Main.call() reads the optional getters by reflection; null here = class not registered
            Object content = Main.descriptionText(Description.EMPTY_DESCRIPTION);
            Object suffix = Main.call(MediaFormat.MPEG_4, "getSuffix");
            require("".equals(content), "description text -> " + content);
            require("mp4".equals(suffix), "MediaFormat.getSuffix -> " + suffix);
            return "description text, MediaFormat.getSuffix";
        });
        ok &= check(checks, "url-handler", () -> {
            // The extractor parses every address with java.net.URL. A native build that lacks the
            // https URL handler fails right here with "unknown protocol: https".
            java.net.URL u = new java.net.URL("https://www.youtube.com/watch?v=dQw4w9WgXcQ");
            require("https".equals(u.getProtocol()) && "www.youtube.com".equals(u.getHost()), "got " + u);
            return u.getProtocol() + " addresses parse";
        });
        ok &= check(checks, "service-urls", SelfTest::serviceUrls);
        if (offline) {
            checks.put("tls", "skipped (--offline)");
        } else {
            ok &= check(checks, "tls", () -> {
                SSLContext ctx = CaBundle.sslContext(caArg);
                Response r = new HttpDownloader(null, ctx).execute(Request.newBuilder().get(target).build());
                require(r.responseCode() >= 200 && r.responseCode() < 400, "HTTP " + r.responseCode());
                return "HTTP " + r.responseCode() + " from " + target + ", CA bundle: " + CaBundle.describe(caArg);
            });
        }

        Map<String, Object> out = new LinkedHashMap<>();
        out.put("kind", "selftest");
        out.put("ok", ok);
        out.put("extractor_version", BuildInfo.EXTRACTOR_VERSION);
        out.put("checks", checks);
        return out;
    }

    /** Offline: the addresses the app hands over most often must be matched to the YouTube service. */
    private static String serviceUrls() throws Exception {
        NewPipe.init(new HttpDownloader(null));
        expectYoutube("https://www.youtube.com/watch?v=dQw4w9WgXcQ", StreamingService.LinkType.STREAM);
        expectYoutube("https://youtu.be/dQw4w9WgXcQ", StreamingService.LinkType.STREAM);
        expectYoutube("https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf",
                StreamingService.LinkType.PLAYLIST);
        return "YouTube video, short link and playlist addresses recognised";
    }

    private static void expectYoutube(String url, StreamingService.LinkType type) throws Exception {
        StreamingService service;
        try {
            service = NewPipe.getServiceByUrl(url);
        } catch (Exception e) {
            // getServiceByUrl() only says "no service"; the link handler holds the real reason
            String why;
            try {
                if (type == StreamingService.LinkType.PLAYLIST) {
                    ServiceList.YouTube.getPlaylistLHFactory().getId(url);
                } else {
                    ServiceList.YouTube.getStreamLHFactory().getId(url);
                }
                why = "the link handler reads the address, but refuses it";
            } catch (Throwable t) {
                why = t.toString();
            }
            throw new IllegalStateException("no service for " + url + " (" + e.getMessage() + "); cause: " + why);
        }
        require(service.getServiceId() == ServiceList.YouTube.getServiceId(),
                url + " went to service " + service.getServiceInfo().getName());
        StreamingService.LinkType got = service.getLinkTypeByUrl(url);
        require(got == type, url + " is " + got + ", expected " + type);
    }

    private static boolean check(Map<String, Object> out, String name, Check c) {
        try {
            out.put(name, "ok: " + c.run());
            return true;
        } catch (Throwable t) {
            out.put(name, "FAILED: " + t);
            return false;
        }
    }

    private static void require(boolean condition, String detail) {
        if (!condition) {
            throw new IllegalStateException(detail);
        }
    }
}
