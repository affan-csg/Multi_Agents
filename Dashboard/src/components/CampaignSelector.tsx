import type { Campaign } from "../agents/outreach";
import { Icon } from "./ui";

interface Props {
  campaigns: Campaign[];
  value: string | "all";
  onChange: (v: string | "all") => void;
}

export function CampaignSelector({ campaigns, value, onChange }: Props) {
  if (campaigns.length === 0) return null;

  return (
    <div className="run-selector">
      <Icon name="send" size={14} />
      <span className="run-selector-label">Campaign:</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="all">All campaigns</option>
        {campaigns.map((c) => (
          <option key={c.campaign_id} value={c.campaign_id}>
            {c.name || c.campaign_id}
          </option>
        ))}
      </select>
    </div>
  );
}
