import java.lang.reflect.Field;
import java.nio.file.Files;
import java.nio.file.Path;
import org.eclipse.jetty.server.Connector;
import org.eclipse.jetty.server.Server;
import org.eclipse.jetty.server.ServerConnector;
import software.amazon.dynamodb.services.local.main.ServerRunner;
import software.amazon.dynamodb.services.local.server.DynamoDBProxyServer;

/** Version-pinned 3.3.1 bridge: bind every DB connector before starting it. */
public final class ArcLocalDynamo {
    public static void main(String[] args) throws Exception {
        if (args.length != 3 || !args[0].matches("[0-9]{1,5}")
                || !args[2].matches("[0-9a-f]{32}")) {
            throw new IllegalArgumentException("Invalid local DB arguments");
        }
        int port = Integer.parseInt(args[0]);
        if (port < 1024 || port > 65535 || !Files.isDirectory(Path.of(args[1]))) {
            throw new IllegalArgumentException("Invalid local DB configuration");
        }
        DynamoDBProxyServer local = ServerRunner.createServerFromCommandLineArgs(
            new String[]{"-sharedDb", "-disableTelemetry", "-dbPath", args[1], "-port", args[0]});
        Field field = DynamoDBProxyServer.class.getDeclaredField("server");
        field.setAccessible(true);
        Server server = (Server) field.get(local);
        Connector[] connectors = server.getConnectors();
        if (connectors.length == 0) throw new IllegalStateException("No DB connector");
        for (Connector connector : connectors) {
            if (!(connector instanceof ServerConnector)) {
                throw new IllegalStateException("Unsupported DB connector");
            }
            ((ServerConnector) connector).setHost("127.0.0.1");
        }
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try { local.stop(); } catch (Exception ignored) { }
        }));
        local.start();
        System.out.println("ARC_DYNAMODB_READY:" + args[2]);
        System.out.flush();
        local.join();
    }
}
