import type { RunSummary } from "../agents/research";
import { Icon } from "./ui";

interface Props {
  runs: RunSummary[];
  value: string | "all";
  onChange: (v: string | "all") => void;
}

export function RunSelector({ runs, value, onChange }: Props) {
  if (runs.length === 0) return null;

  return (
    <div className="run-selector">
      <Icon name="play" size={14} />
      <span className="run-selector-label">Research:</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="all">All research</option>
        {runs.map((r) => (
          <option key={r.run_id} value={r.run_id}>
            {r.label || r.run_id}
          </option>
        ))}
      </select>
    </div>
  );
}
