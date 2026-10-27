export default function AnalysisActivity() {
  return <div className="analysis-activity" role="status" aria-label="Analyzing folder">
    <span className="sr-only">Analyzing folder…</span>
    <span className="analysis-light-bar" aria-hidden="true">
      <span className="analysis-light-beam" />
    </span>
  </div>;
}
