/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { GitDelivery, GitDeliveryPolicy } from "./GitDeliveryData";

export type Connection = {
  base: string;
  token: string;
  authenticated?: boolean;
};
export type Repository = {
  id: string;
  name: string;
  url: string;
  provider: string;
  default_branch: string;
  connection_provider?: string;
  remote_repository_id?: string;
};
import type { DiffReference } from "./GitDiffData";
export type ForgeRun = {
  diff_artifact?: DiffReference | null;
  diff_preview_bytes?: number;
  diff_preview_truncated?: boolean;
  git_delivery_policy?: GitDeliveryPolicy;
  git_delivery_summary?: Pick<
    GitDelivery,
    "revision" | "source_tree" | "sequence" | "remote_status" | "pr_url"
  >;
  project_id?: string | null;
  component_id?: string | null;
  publish?: boolean;
  id: string;
  repository_id: string;
  brief: string;
  status: string;
  active_task?: string;
  attempt?: number;
  provider_recovery?: unknown;
  invalid_read_continuity?: unknown;
  usage?: Record<string, unknown>;
  implementation_pending?: boolean;
  backend?: string;
  frontend?: string;
  agent?: string;
  operation?: "build" | "maintain" | "test";
  skills?: string[];
  created_at?: string;
  events?: Array<Record<string, unknown>>;
  verification?: Array<Record<string, unknown>> | Record<string, unknown>;
  changed_files?: string[];
  diff?: string;
  artifacts?: unknown[];
  error?: string | null;
  pr_url?: string | null;
  source_tree?: string;
  checkpoint_revision?: string;
  branch?: string;
  publication_request?: { id: string; status: string };
  cancel_request?: { id: string; status: string };
  review?: {
    status: string;
    source_tree: string;
    summary: string;
    findings: {
      severity: string;
      path?: string;
      line?: number;
      evidence: string;
      summary: string;
    }[];
  };
  issue_context?: IssueContext | null;
  product_definition?: ProductSnapshot | null;
  bridge_completion_hold?: { reason?: string };
  verification_handoff?: unknown;
  verification_refusal?: unknown;
  worker_request?: { id: string; kind: string; status: string };
  pause?: {
    message: string;
    kind: string;
    automatic: boolean;
    retry_at?: number;
  };
};
export type NewRun = {
  repository_id: string;
  brief: string;
  backend: string;
  frontend: string;
  agent: string;
  commands: { argv: string[]; timeout: number | null }[];
  publish: false;
};
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public responseKind: "http" | "unreadable" = "http",
    public code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export function normalizeBase(value: string): string {
  const candidate = value.trim().replace(/\/+$/, "") || "/api/v1";
  if (candidate === "/api/v1") return candidate;
  let url: URL;
  try {
    url = new URL(candidate);
  } catch {
    throw new Error("Use /api/v1 or a complete HTTP(S) API address.");
  }
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error(
      "Use an HTTP(S) API address without credentials, query or fragment.",
    );
  return candidate;
}
export async function request<T>(
  connection: Connection,
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  let csrf = "";
  if (
    !connection.token &&
    !["GET", "HEAD", "OPTIONS"].includes(method) &&
    typeof document !== "undefined"
  ) {
    const cookie = document.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith("XSRF-TOKEN="));
    if (cookie) {
      try {
        csrf = decodeURIComponent(cookie.slice("XSRF-TOKEN=".length));
      } catch {
        csrf = "";
      }
    }
  }
  const response = await fetch(normalizeBase(connection.base) + path, {
    ...options,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(csrf ? { "X-XSRF-TOKEN": csrf } : {}),
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(connection.token
        ? { Authorization: "Bearer " + connection.token }
        : {}),
      ...options.headers,
    },
  });
  const contents = await response.text();
  let data: unknown;
  try {
    data = contents ? JSON.parse(contents) : undefined;
  } catch {
    throw new ApiError(
      response.ok
        ? "The server returned a non-JSON response. Check the API address."
        : `Request failed (${response.status}).`,
      response.status,
      "unreadable",
    );
  }
  if (!response.ok) {
    if (
      response.status === 401 &&
      connection.authenticated &&
      typeof window !== "undefined"
    )
      window.dispatchEvent(new Event("forge-session-expired"));
    const error = data as
      { detail?: unknown; error?: string; code?: unknown } | undefined;
    const validation = Array.isArray(error?.detail)
      ? error.detail
          .flatMap((item: unknown) => {
            if (!item || typeof item !== "object") return [];
            const entry = item as { loc?: unknown; msg?: unknown };
            if (
              !Array.isArray(entry.loc) ||
              typeof entry.msg !== "string" ||
              entry.loc.some(
                (part: unknown) =>
                  typeof part !== "string" && typeof part !== "number",
              )
            )
              return [];
            const path = entry.loc
              .filter((part: unknown) => part !== "body")
              .slice(0, 8)
              .map((part: string | number) => String(part).slice(0, 64))
              .join(".");
            const message = entry.msg.replace(/[\r\n\t]/g, " ").slice(0, 240);
            return [path ? `${path}: ${message}` : message];
          })
          .slice(0, 4)
          .join("; ")
          .slice(0, 1400)
      : "";
    const detail =
      typeof error?.detail === "string"
        ? error.detail
        : validation || error?.error;
    throw new ApiError(
      detail ||
        (response.status === 401
          ? "Your session is unavailable. Sign in again to continue."
          : `Request failed (${response.status}).`),
      response.status,
      typeof error?.detail === "string" ||
        validation ||
        typeof error?.error === "string"
        ? "http"
        : "unreadable",
      typeof error?.code === "string" && /^[a-z_]{1,100}$/.test(error.code)
        ? error.code
        : undefined,
    );
  }
  return data as T;
}
export const post = <T>(connection: Connection, path: string, body?: unknown) =>
  request<T>(connection, path, {
    method: "POST",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
export function parseCommands(
  value: string,
): { argv: string[]; timeout: number }[];
export function parseCommands(
  value: string,
  options: { workDurationV2: true },
): NewRun["commands"];
export function parseCommands(
  value: string,
  options?: { workDurationV2: true },
): NewRun["commands"] {
  if (!value.trim()) return [];
  let commands: unknown;
  try {
    commands = JSON.parse(value);
  } catch {
    throw new Error(
      "Verification commands must be a JSON array of argument arrays.",
    );
  }
  if (
    !Array.isArray(commands) ||
    commands.some(
      (command) =>
        !Array.isArray(command) ||
        !command.length ||
        command.some(
          (argument) => typeof argument !== "string" || !argument.length,
        ),
    )
  )
    throw new Error(
      "Each command needs at least one non-empty string argument.",
    );
  return (commands as string[][]).map((argv) => ({
    argv,
    timeout: options?.workDurationV2 === true ? null : 120,
  }));
}
export type MetricThreshold = {
  name: string;
  operator: "gt" | "lt";
  value: number;
};
export type ApplicationObservation = {
  id: string;
  application_id: string;
  status: "healthy" | "degraded" | "unavailable";
  latency_ms: number;
  metrics: Record<string, number>;
  reasons: string[];
  created_at: string;
};
export type ApplicationIncident = {
  id: string;
  application_id: string;
  repository_id: string;
  target_id: string;
  status: "open" | "resolved";
  summary: string;
  created_at: string;
  updated_at: string;
  latest_observation_id: string;
  maintenance_run_ids: string[];
  resolution?: string;
  resolved_at?: string;
};
export type MonitoredApplication = {
  backend: string;
  frontend: string;
  id: string;
  name: string;
  repository_id: string;
  target_id: string;
  health_url: string;
  metrics_url: string | null;
  interval_seconds: number;
  max_latency_ms: number;
  metric_thresholds: MetricThreshold[];
  observability: {
    logs_url?: string;
    traces_url?: string;
    dashboard_url?: string;
  };
  latest: ApplicationObservation | null;
  stale: boolean;
  incidents: ApplicationIncident[];
  observations?: ApplicationObservation[];
};
export type ProductComponent = {
  name: string;
  layer: "domain" | "application" | "data" | "web" | "frontend" | "operations";
  responsibility: string;
};
export type ProductArchitecture = {
  backend: string;
  frontend: string;
  components: ProductComponent[];
  decisions: string[];
  nonfunctional_requirements: string[];
  reviewed: boolean;
};
export type ProductDesignReview = {
  method?: "wireframe" | "process_map";
  digest: string;
  reviewed: true;
  screen_ids: string[];
  viewports: string[];
  themes: string[];
};
export type ProductProcess = {
  id: string;
  name: string;
  actor: string;
  trigger: string;
  journey_refs: string[];
  acceptance_refs: string[];
  steps: {
    id: string;
    action: string;
    owner: string;
    inputs: string[];
    outputs: string[];
    failures: string[];
  }[];
};
export type ProductVisualDesign = {
  renderer: "product-reference-template-v2" | "fixed-reference-template-v1";
  geometry_configurable: boolean;
  baseline: 2 | 4 | 8;
  desktop: {
    viewport_width: number;
    viewport_height?: number;
    columns: number;
    gutter: number;
    margin: number;
  };
  mobile: {
    viewport_width: number;
    viewport_height?: number;
    columns: number;
    gutter: number;
    margin: number;
  };
  shell: {
    header_height: number;
    sidebar_width: number;
    footer_height: number;
    max_content_width: number;
  };
  atomic_hierarchy: string[];
  states: string[];
  accessibility: string[];
  guidance: string[];
};
export type ProductAIOrigin = {
  conversation_id: string;
  turn_id: string;
  proposal_digest: string;
};
export type AIProposalProvenance = ProductAIOrigin & {
  kind: "ai_generated";
  provider: "bridge" | "agentic";
  model: string | null;
  generated_at: string;
};
export type ProductSavedOrigin = Partial<Omit<AIProposalProvenance, "kind">> & {
  kind: "manual" | "ai_assisted";
  edited?: boolean;
};
export type ProductIntegration = {
  id: string;
  name: string;
  kind: "owned" | "third_party";
  protocol: "http" | "event" | "queue" | "database";
  endpoint?: string | null;
  service_ref?: string | null;
  header_names?: string[];
  authentication: {
    method: "none" | "oauth2" | "api_key" | "mtls" | "workload_identity";
    secret_refs: string[];
  };
  inputs: {
    name: string;
    type: "string" | "integer" | "number" | "boolean" | "object" | "array";
    required?: boolean;
    description?: string;
  }[];
  outputs: {
    name: string;
    type: "string" | "integer" | "number" | "boolean" | "object" | "array";
    required?: boolean;
    description?: string;
  }[];
  failure_paths: string[];
  retry: { max_attempts: number; backoff_seconds: number; retry_on: string[] };
  idempotency: {
    mode: "required" | "optional" | "not_applicable";
    key_field?: string | null;
    key_location: "header" | "body";
  };
  side_effects?: boolean;
  feature_refs: string[];
  journey_refs: string[];
};
export type CustomerProductDefinition = {
  ai_origin?: ProductAIOrigin | null;
  customer: string;
  problem: string;
  journey: string[];
  user_stories: string[];
  acceptance: string[];
  nonfunctional_requirements: string[];
  security_requirements: string[];
  owners: string[];
  features?: FunctionalFeature[];
  integrations?: ProductIntegration[];
  functional_reviewed?: boolean;
  processes?: ProductProcess[];
  visual_design?: ProductVisualDesign;
  architecture?: ProductArchitecture | null;
  design_review?: ProductDesignReview | null;
};
export type ProductSnapshot = {
  change_receipt?: {
    request_id: string;
    request_digest: string;
    base_revision: number;
    base_digest: string;
    implementation_status: "not_started";
  };
  origin?: ProductSavedOrigin;
  repository_id: string;
  revision: number;
  definition: CustomerProductDefinition;
  digest: string;
  created_at: string;
};
export type ProductBrand = {
  repository_id: string;
  revision: string;
  digest: string;
  name: string;
  font_family: "Manrope";
  files: Record<string, string>;
  tokens: Record<string, unknown>;
};
export type IssueContext = {
  provider: string;
  receipt_id: string;
  actor: string;
  kind: string;
  number: number;
  title?: string;
  url?: string;
};
export type ProjectComponent = {
  id: string;
  name: string;
  repository_id: string;
  path: string;
  kind: "api" | "web" | "worker" | "library" | "fullstack";
  backend: string;
  frontend: string;
  verification: { argv: string[]; timeout: number }[];
  depends_on: string[];
  responsibility: string;
};
export type ProjectDefinitionData = {
  name: string;
  description: string;
  topology: "monorepo" | "multi_repo";
  owners: string[];
  product_repository_id?: string;
  components: ProjectComponent[];
};
export type ProjectSnapshot = {
  id: string;
  definition: ProjectDefinitionData;
  revision: number;
  digest: string;
  created_at: string;
  updated_at: string;
};
export type ComponentRunContext = {
  project: ProjectSnapshot;
  component: ProjectComponent;
};
export type FunctionalFeature = {
  id: string;
  name: string;
  journey_refs: string[];
  acceptance_refs: string[];
  component_refs: string[];
  behaviors: {
    id: string;
    given: string;
    when: string;
    then: string;
    failure_paths: string[];
  }[];
};
