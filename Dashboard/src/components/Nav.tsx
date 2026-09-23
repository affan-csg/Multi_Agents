import { useEffect, useState } from "react";
import { Icon, type IconName } from "./ui";
import type { Route } from "../router";
import { outreachApi } from "../agents/outreach";

interface NavItem {
  route: Route;
  label: string;
  icon: IconName;
  badge?: number;
}

interface NavSection {
  title: string;
  icon: IconName;
  items: NavItem[];
}

function useSections(): NavSection[] {
  const [prospectCount, setProspectCount] = useState<number | null>(null);

  useEffect(() => {
    outreachApi.listProspects().then((list) => setProspectCount(list.length)).catch(() => {});
  }, []);

  return [
    {
      title: "Research Agent",
      icon: "search",
      items: [
        { route: { name: "research-runs" }, label: "Runs", icon: "play" },
        { route: { name: "research-companies" }, label: "Companies", icon: "building" },
        { route: { name: "research-saved" }, label: "Saved", icon: "bookmark" },
        { route: { name: "research-contacts" }, label: "Contacts", icon: "contact" },
        { route: { name: "research-analytics" }, label: "Analytics", icon: "analytics" },
        { route: { name: "research-settings" }, label: "Settings", icon: "settings" },
      ],
    },
    {
      title: "Outreach Agent",
      icon: "send",
      items: [
        { route: { name: "outreach-prospects" }, label: "Prospects", icon: "target", badge: prospectCount ?? undefined },
        { route: { name: "outreach-campaigns" }, label: "Campaigns", icon: "send" },
        { route: { name: "outreach-bookings" }, label: "Bookings", icon: "calendar" },
        { route: { name: "outreach-analytics" }, label: "Analytics", icon: "analytics" },
        { route: { name: "outreach-learning" }, label: "Learning & Optimization", icon: "brain" },
      ],
    },
  ];
}

function routeGroup(name: Route["name"]): string {
  if (name.startsWith("research-")) return "research-companies".startsWith(name) ? name : name;
  return name;
}

interface Props {
  currentRoute: Route;
  onNavigate: (r: Route) => void;
  collapsed: boolean;
  onToggle: () => void;
  apiStatus: Record<string, boolean | null>;
}

export function Nav({ currentRoute, onNavigate, collapsed, onToggle, apiStatus }: Props) {
  const sections = useSections();
  const activeName =
    currentRoute.name === "research-company" ? "research-companies"
    : currentRoute.name === "outreach-campaign" ? "outreach-campaigns"
    : currentRoute.name;

  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>({});
  const toggleSection = (title: string) =>
    setCollapsedSections((s) => ({ ...s, [title]: !s[title] }));

  const anyDown = Object.values(apiStatus).some((v) => v === false);
  const allChecked = Object.values(apiStatus).every((v) => v !== null);

  return (
    <nav className="nav">
      <button
        type="button"
        className="nav-collapse"
        onClick={onToggle}
        aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
      >
        <Icon name={collapsed ? "chevronRight" : "chevronLeft"} size={14} />
      </button>

      <div className="nav-brand">
        <span className="nav-logo">
          <Icon name="database" size={19} />
        </span>
        <span className="nav-brand-name">CSG Multi Agents</span>
      </div>

      <div className="nav-scroll">
        <button
          type="button"
          className={`nav-item${currentRoute.name === "home" ? " nav-item--active" : ""}`}
          onClick={() => onNavigate({ name: "home" })}
          title={collapsed ? "Overview" : undefined}
        >
          <Icon name="home" size={19} />
          <span className="nav-item-label">Overview</span>
        </button>

        {sections.map((section) => {
          const sectionActive = section.items.some((item) => routeGroup(activeName) === item.route.name);
          const isCollapsed = !!collapsedSections[section.title];
          return (
            <div
              className={`nav-section${sectionActive ? " nav-section--active" : ""}${isCollapsed ? " nav-section--collapsed" : ""}`}
              key={section.title}
            >
              <button
                type="button"
                className="nav-section-head"
                onClick={() => (collapsed ? onNavigate(section.items[0].route) : toggleSection(section.title))}
                title={collapsed ? section.title : undefined}
              >
                <span className="nav-section-icon">
                  <Icon name={section.icon} size={17} />
                </span>
                <span className="nav-section-head-label">{section.title}</span>
                {sectionActive && <span className="nav-section-dot" />}
                <span className="nav-section-chevron">
                  <Icon name="chevronDown" size={14} />
                </span>
              </button>

              {!isCollapsed && (
                <div className="nav-section-tree">
                  {section.items.map((item) => (
                    <div className="nav-tree-row" key={item.label}>
                      <button
                        type="button"
                        className={`nav-item${routeGroup(activeName) === item.route.name ? " nav-item--active" : ""}`}
                        onClick={() => onNavigate(item.route)}
                        title={collapsed ? item.label : undefined}
                      >
                        <Icon name={item.icon} size={17} />
                        <span className="nav-item-label">{item.label}</span>
                        {item.badge != null && <span className="nav-badge">{item.badge}</span>}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="nav-bottom">
        <div className={`nav-status${anyDown ? " nav-status--down" : ""}`}>
          <span className="nav-status-dot" />
          <div className="nav-status-text">
            <div className="nav-status-title">
              {!allChecked ? "Checking agents…" : anyDown ? "One or more agents unreachable" : "All agents online"}
            </div>
            <div className="nav-status-sub">Research · Outreach</div>
          </div>
        </div>

        <div className="nav-footer">
          <span className="nav-tagline-accent">One dashboard.</span> Every agent.
        </div>
      </div>
    </nav>
  );
}
