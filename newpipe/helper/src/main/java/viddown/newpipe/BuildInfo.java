package viddown.newpipe;

/**
 * The two EXTRACTOR_* values are rewritten by scripts/build/build_newpipe.py before it compiles,
 * from native/sources.lock.toml. These are the values of a plain javac run.
 */
final class BuildInfo {
    static final String EXTRACTOR_VERSION = "unknown";
    static final String EXTRACTOR_COMMIT = "unknown";
    /** Bump when the JSON the "info" command prints changes in a way the app has to know about. */
    static final int HELPER_API = 1;

    private BuildInfo() { }
}
