import { useEffect, useState } from "react";

export type Route =
  | { name: "home" }
  | { name: "research-runs" }
  | { name: "research-companies" }
  | { name: "research-company"; domain: string }
  | { name: "research-saved" }
  | { name: "research-contacts" }
  | { name: "research-analytics" }
  | { name: "research-settings" }
  | { name: "outreach-prospects" }
  | { name: "outreach-campaigns" }
  | { name: "outreach-campaign"; campaignId: string; campaignName?: string | null }
  | { name: "outreach-bookings" }
  | { name: "outreach-analytics" };

const SIMPLE_ROUTES = [
  "research-runs", "research-saved", "research-contacts", "research-analytics", "research-settings",
  "outreach-prospects", "outreach-campaigns", "outreach-bookings", "outreach-analytics",
] as const;

// Campaign URLs are `/outreach/campaigns/{slug}-{uuid}` -- the UUID is the
// real identity (used for every API call), the slug in front is cosmetic
// (derived from the campaign's name) so the URL reads like a name instead of
// a bare ID. A UUID is 36 chars (8-4-4-4-12); anything trailing that is the
// slug and gets discarded on parse -- we always re-derive it fresh from the
// current campaign name rather than trusting what's in the URL.
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function campaignIdFromSlug(slugOrId: string): string {
  const match = slugOrId.match(UUID_RE);
  return match ? match[0] : slugOrId;
}

// Best-effort display name recovered from the URL's slug prefix, so a
// direct link / page reload doesn't have to wait on the campaigns cache to
// show *something* sensible in the header instead of the raw UUID. This is
// only ever a fallback -- the campaigns fetch result is the source of truth
// and overrides it once loaded.
function nameFromSlug(slugOrId: string): string | null {
  const match = slugOrId.match(UUID_RE);
  if (!match) return null;
  const prefix = slugOrId.slice(0, match.index).replace(/-+$/, "");
  if (!prefix) return null;
  return prefix.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

export function parseRoute(path: string): Route {
  const parts = path.replace(/^\/+|\/+$/g, "").split("/");
  const head = parts[0];
  const sub = parts[1];

  if (head === "research" && sub === "companies") {
    return parts[2] ? { name: "research-company", domain: decodeURIComponent(parts[2]) } : { name: "research-companies" };
  }
  if (head === "outreach" && sub === "campaigns") {
    if (!parts[2]) return { name: "outreach-campaigns" };
    const raw = decodeURIComponent(parts[2]);
    return { name: "outreach-campaign", campaignId: campaignIdFromSlug(raw), campaignName: nameFromSlug(raw) };
  }
  const joined = sub ? `${head}-${sub}` : head;
  if ((SIMPLE_ROUTES as readonly string[]).includes(joined)) {
    return { name: joined as (typeof SIMPLE_ROUTES)[number] };
  }
  return { name: "home" };
}

export function routeToPath(route: Route): string {
  switch (route.name) {
    case "home":
      return "/";
    case "research-company":
      return `/research/companies/${encodeURIComponent(route.domain)}`;
    case "research-companies":
      return "/research/companies";
    case "research-runs":
      return "/research/runs";
    case "research-saved":
      return "/research/saved";
    case "research-settings":
      return "/research/settings";
    case "research-contacts":
      return "/research/contacts";
    case "research-analytics":
      return "/research/analytics";
    case "outreach-campaign": {
      const slug = route.campaignName ? slugify(route.campaignName) : "";
      const segment = slug ? `${slug}-${route.campaignId}` : route.campaignId;
      return `/outreach/campaigns/${encodeURIComponent(segment)}`;
    }
    case "outreach-campaigns":
      return "/outreach/campaigns";
    case "outreach-prospects":
      return "/outreach/prospects";
    case "outreach-bookings":
      return "/outreach/bookings";
    case "outreach-analytics":
      return "/outreach/analytics";
  }
}

export function useRoute(): [Route, (r: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));

  useEffect(() => {
    const onPop = () => setRoute(parseRoute(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  function navigate(next: Route) {
    const path = routeToPath(next);
    if (path !== window.location.pathname) window.history.pushState(null, "", path);
    setRoute(next);
  }

  return [route, navigate];
}
