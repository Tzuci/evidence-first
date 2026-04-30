async function fetchHealth(): Promise<{ db?: string; redis?: string; storage?: string; error?: string }> {
  try {
    const res = await fetch(`http://localhost:${process.env.WEB_PORT ?? 3000}/api/proxy-health`, {
      cache: "no-store",
    });
    if (!res.ok) {
      return { error: `proxy-health returned status ${res.status}` };
    }
    return await res.json();
  } catch (e: unknown) {
    const message = e instanceof Error ? e.message : "unknown error";
    return { error: message };
  }
}

export const dynamic = "force-dynamic";

export default async function DiagnosticPage() {
  const data = await fetchHealth();
  const ok = (k: "db" | "redis" | "storage") => data[k] === "ok";

  if (data.error) {
    return (
      <section>
        <h1>Diagnostic</h1>
        <p style={{ color: "#a00" }}>API unreachable.</p>
        <pre style={{ background: "#fff3f3", padding: 12, borderRadius: 4 }}>{data.error}</pre>
        <p>
          <a href="/">Back</a>
        </p>
      </section>
    );
  }

  return (
    <section>
      <h1>Diagnostic</h1>
      <table style={{ borderCollapse: "collapse", marginTop: 12 }}>
        <tbody>
          {(["db", "redis", "storage"] as const).map((k) => (
            <tr key={k}>
              <td style={{ padding: "6px 12px", border: "1px solid #ddd", textTransform: "uppercase" }}>{k}</td>
              <td
                style={{
                  padding: "6px 12px",
                  border: "1px solid #ddd",
                  color: ok(k) ? "#0a7d2c" : "#a00",
                  fontWeight: 600,
                }}
                aria-label={`${k} ${ok(k) ? "ok" : "not ok"}`}
              >
                {ok(k) ? "OK" : data[k] ?? "fail"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p style={{ marginTop: 16 }}>
        <a href="/">Back</a>
      </p>
    </section>
  );
}