export default function AnalysisActivity() {
  return <div className="analysis-activity" role="status" aria-label="Analyzing folder">
    <span className="sr-only">Analyzing folder…</span>
    <svg className="analysis-light-bar" viewBox="0 0 160 16" aria-hidden="true" focusable="false">
      <path className="analysis-light-wave" d="M-80 8 Q-60 1 -40 8 T0 8 T40 8 T80 8 T120 8 T160 8 T200 8 T240 8" />
    </svg>
  </div>;
}
