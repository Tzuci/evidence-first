export default function HomePage() {
  return (
    <section>
      <h1>Evidence-First Multi-AI Platform</h1>
      <p>
        This environment is running in <strong>MVP-0</strong> mode. The system does not call any
        external AI provider, and all responses are produced from your local corpus only.
      </p>
      <p>
        No claim is published unless it is linked to a retrievable evidence span, recorded in the
        Claim Ledger, verified by the Citation Span Verifier, and approved by the Final Answer Gate.
      </p>
      <ul>
        <li>
          <a href="/diagnostic">/diagnostic</a> — service health
        </li>
      </ul>
      <hr />
      <p style={{ fontSize: 12, color: "#555" }}>
        Phase 8.1b: API + Worker + Web stub. No real document upload yet (Phase 8.2).
      </p>
    </section>
  );
}