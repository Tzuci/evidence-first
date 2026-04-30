export const metadata = {
  title: "Evidence-First MVP-0",
  description: "Evidence-first multi-AI platform — MVP-0 (MockProvider only)",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
          margin: 0,
          padding: 0,
          color: "#111",
          background: "#fafafa",
        }}
      >
        <div
          role="status"
          aria-label="Environment banner"
          style={{
            background: "#1f2937",
            color: "#fff",
            padding: "8px 16px",
            fontSize: 13,
            letterSpacing: 0.3,
          }}
        >
          MVP-0 · MockProvider · No external AI calls · Cost API: $0
        </div>
        <main style={{ maxWidth: 880, margin: "0 auto", padding: "32px 24px" }}>{children}</main>
      </body>
    </html>
  );
}