package viddown.newpipe;

import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManagerFactory;
import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.cert.CertificateFactory;
import java.util.Base64;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Trusted CA certificates from a PEM file (the certifi cacert.pem that ships next to the binary).
 *
 * Why not the JVM's own cacerts: a native executable has no JDK folder to read it from, and where the
 * default trust store is initialised (build time or run time) differs between GraalVM versions.
 * Loading a PEM ourselves and handing the SSLContext to the HTTP client does not depend on that.
 *
 * Lookup order: --cacert, VIDDOWN_CACERT, SSL_CERT_FILE, cacert.pem next to the executable.
 * Nothing found = null = the Java default (right when running as "java -jar").
 */
final class CaBundle {

    private static final Pattern CERT = Pattern.compile(
            "-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", Pattern.DOTALL);

    private CaBundle() { }

    static Path find(String explicit) {
        for (String candidate : new String[] {explicit, System.getenv("VIDDOWN_CACERT"), System.getenv("SSL_CERT_FILE")}) {
            if (candidate != null && !candidate.isBlank()) {
                Path p = Path.of(candidate.trim());
                if (Files.isRegularFile(p)) {
                    return p;
                }
                if (candidate == explicit) {
                    throw new IllegalArgumentException("CA bundle not found: " + candidate);
                }
            }
        }
        try {
            String exe = ProcessHandle.current().info().command().orElse(null);
            if (exe != null) {
                Path dir = Path.of(exe).toAbsolutePath().getParent();
                if (dir != null && Files.isRegularFile(dir.resolve("cacert.pem"))) {
                    return dir.resolve("cacert.pem");
                }
            }
        } catch (RuntimeException ignored) {
            // no process info on this platform: use the Java default
        }
        return null;
    }

    /** SSLContext trusting exactly the certificates of the PEM found by {@link #find}; null = Java default. */
    static SSLContext sslContext(String explicit) throws Exception {
        Path pem = find(explicit);
        if (pem == null) {
            return null;
        }
        KeyStore store = KeyStore.getInstance(KeyStore.getDefaultType());
        store.load(null, null);
        CertificateFactory factory = CertificateFactory.getInstance("X.509");
        String text = new String(Files.readAllBytes(pem), StandardCharsets.US_ASCII);
        Matcher m = CERT.matcher(text);
        int n = 0;
        while (m.find()) {
            byte[] der = Base64.getMimeDecoder().decode(m.group(1));
            store.setCertificateEntry("ca-" + n++, factory.generateCertificate(new ByteArrayInputStream(der)));
        }
        if (n == 0) {
            throw new IllegalArgumentException("No certificate found in " + pem);
        }
        TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init(store);
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(null, tmf.getTrustManagers(), null);
        return ctx;
    }

    /** For the selftest report: which source the trust store comes from. */
    static String describe(String explicit) {
        Path pem = find(explicit);
        return pem == null ? "Java default trust store" : pem.toString();
    }
}
