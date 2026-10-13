/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { repositoryLabels } from "./RepositoryLabels";
import { useEffect, useState, type FormEvent } from "react";
import {
  post,
  request,
  normalizeBase,
  type Connection,
  type Repository,
} from "./api";
import {
  CredentialProfile,
  type DeploymentCredential,
} from "./CredentialProfile";
import { Wizard } from "./Wizard";
import { SetupIllustration } from "./SetupIllustration";
type Provider = {
  id: string;
  name: string;
  tool: string;
  template_version: string;
  provider_version?: string;
  fields: { key: string; required: boolean; default: string }[];
  credential_names: string[];
};
type Recipe = {
  id: string;
  provider: string;
  files: Record<string, string>;
  content_digest: string;
  requirements: string[];
  validation: {
    state: string;
    infrastructure_applied: boolean;
    cloud_account_verified: boolean;
  };
  target: { name: string; repository_id: string };
};
const failure = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The cloud recipe could not be rendered.";
const labels: Record<string, string> = {
  region: "Region",
  vpc_id: "Existing VPC ID",
  subnet_id: "Existing subnet ID",
  image_id: "Operating system image ID",
  state_bucket: "Existing state bucket",
  state_key: "Remote state key",
  subscription_id: "Azure subscription ID",
  state_resource_group: "State resource group",
  state_storage_account: "State storage account",
  state_container: "State container",
  project: "GCP project ID",
  zone: "Zone",
  state_region: "State bucket region",
  state_endpoint: "State storage HTTPS endpoint",
  ssh_key_id: "Existing SSH key ID",
  host: "Dedicated host",
  size: "Machine size",
};
export function CloudRecipe({
  connection,
  repositories,
  onConfigured,
}: {
  connection: Connection;
  repositories: Repository[];
  onConfigured: () => void;
}) {
  const projectLabels = repositoryLabels(repositories);
  const [editingCredential, setEditingCredential] = useState(false);
  const [credentialCatalog, setCredentialCatalog] = useState<
    DeploymentCredential[]
  >([]);
  useEffect(() => {
    const controller = new AbortController();
    request<DeploymentCredential[]>(connection, "/deployment-credentials", {
      signal: controller.signal,
    })
      .then(setCredentialCatalog)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(failure(cause));
      });
    return () => controller.abort();
  }, [connection]);
  const [catalog, setCatalog] = useState<Provider[]>([]);
  const [provider, setProvider] = useState("");
  const [project, setProject] = useState("");
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [image, setImage] = useState("");
  const [previous, setPrevious] = useState("");
  const [domain, setDomain] = useState("");
  const [health, setHealth] = useState("/health");
  const [port, setPort] = useState(8080);
  const [user, setUser] = useState("ubuntu");
  const [ssh, setSSH] = useState("");
  const [cidr, setCIDR] = useState("");
  const [profile, setProfile] = useState("");
  const [config, setConfig] = useState<Record<string, string>>({});
  const [rendered, setRendered] = useState<Recipe | null>(null);
  const [saved, setSaved] = useState<Recipe | null>(null);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    request<Provider[]>(connection, "/cloud/providers", {
      signal: controller.signal,
    })
      .then(setCatalog)
      .catch((cause) => {
        if (!controller.signal.aborted) setError(failure(cause));
      });
    return () => controller.abort();
  }, [connection]);
  const selected = catalog.find((item) => item.id === provider);
  const payload = () => ({
    provider,
    repository_id: project,
    name,
    image,
    previous_image: previous || null,
    domain,
    health_path: health,
    container_port: port,
    ssh_user: user,
    ssh_public_key: ssh,
    admin_cidr: cidr,
    credential_profile: profile || null,
    config,
  });
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (step === 1) {
        const recipe = await post<Recipe>(
          connection,
          "/cloud/recipes/render",
          payload(),
        );
        setRendered(recipe);
      }
      if (step < 3) {
        setStep(step + 1);
        return;
      }
      if (!credentialCatalog.some((item) => item.id === profile))
        throw new Error(
          "Configure a private credential profile before saving this target.",
        );
      const recipe =
        saved ?? (await post<Recipe>(connection, "/cloud/recipes", payload()));
      setSaved(recipe);
      await post(connection, `/cloud/recipes/${recipe.id}/configure-target`, {
        target_id: target,
      });
      setNotice(
        "Recipe saved and target configured. Infrastructure has not been applied. Deploy a verified run explicitly from its Deployment tab.",
      );
      onConfigured();
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  }
  async function download() {
    if (!saved) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `${normalizeBase(connection.base)}/cloud/recipes/${saved.id}/archive.zip`,
        {
          credentials: "include",
          headers: connection.token
            ? { Authorization: `Bearer ${connection.token}` }
            : {},
        },
      );
      if (!response.ok)
        throw new Error(`Recipe export failed (${response.status}).`);
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "deployment-recipe.zip";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      setError(failure(cause));
    } finally {
      setBusy(false);
    }
  }
  const input = (
    label: string,
    id: string,
    value: string,
    setter: (value: string) => void,
    required = true,
  ) => (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        value={value}
        required={required}
        onChange={(event) => setter(event.target.value)}
      />
    </div>
  );
  return (
    <div className="cloud-recipe">
      {editingCredential && (
        <CredentialProfile
          connection={connection}
          keys={selected?.credential_names ?? []}
          initial={profile}
          onClose={() => setEditingCredential(false)}
          onSaved={(value) => {
            setProfile(value.id);
            setCredentialCatalog((items) => [
              ...items.filter((item) => item.id !== value.id),
              value,
            ]);
          }}
        />
      )}
      <Wizard
        locked={busy}
        steps={["Provider", "Infrastructure", "Recipe", "Target"]}
        active={step}
        onStep={setStep}
        title={
          [
            "Choose the deployment destination",
            "Configure the host and remote state",
            "Review the rendered recipe",
            "Save an explicit deployment target",
          ][step]
        }
        description={
          [
            "Render a provider-specific recipe for an immutable application image.",
            "Use existing network and state resources. Credentials remain in an operator-managed private profile.",
            "Inspect the actual files, tool versions and requirements. Rendering does not contact a cloud account or deploy software.",
            "Saving records the recipe and configures a target. Explicit deployment applies infrastructure and verifies application health.",
          ][step]
        }
      >
        <SetupIllustration kind="cloud" step={step} />
        <form onSubmit={(event) => void submit(event)}>
          {step === 0 && (
            <>
              <div className="field">
                <label htmlFor="cloud-project">Cloud project</label>
                <select
                  id="cloud-project"
                  required
                  value={project}
                  onChange={(event) => setProject(event.target.value)}
                >
                  <option value="">Choose a repository</option>
                  {repositories.map((repo) => (
                    <option key={repo.id} value={repo.id}>
                      {projectLabels.get(repo.id)}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="cloud-provider">Cloud or host provider</label>
                <select
                  id="cloud-provider"
                  required
                  value={provider}
                  onChange={(event) => {
                    setProvider(event.target.value);
                    const chosen = catalog.find(
                      (item) => item.id === event.target.value,
                    );
                    setConfig(
                      Object.fromEntries(
                        chosen?.fields
                          .filter((field) => field.default)
                          .map((field) => [field.key, field.default]) ?? [],
                      ),
                    );
                    setRendered(null);
                    setSaved(null);
                  }}
                >
                  <option value="">Choose a provider</option>
                  {catalog.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </div>
              {input("Recipe name", "cloud-name", name, setName)}
              {input("Immutable image digest", "cloud-image", image, setImage)}
              <small>
                Use registry/repository@sha256 followed by the 64-character
                image digest.
              </small>
              {input(
                "Previous image digest for rollback",
                "cloud-previous",
                previous,
                setPrevious,
                false,
              )}
              {input("Application domain", "cloud-domain", domain, setDomain)}
            </>
          )}
          {step === 1 && (
            <>
              {selected?.fields.map((field) =>
                input(
                  labels[field.key] ?? field.key,
                  `cloud-${field.key}`,
                  config[field.key] ?? "",
                  (value) =>
                    setConfig((current) => ({
                      ...current,
                      [field.key]: value,
                    })),
                  field.required,
                ),
              )}
              <div className="two-columns">
                {input("SSH user", "cloud-user", user, setUser)}
                {input("Administration IPv4 CIDR", "cloud-cidr", cidr, setCIDR)}
              </div>
              <div className="field">
                <label htmlFor="cloud-key">SSH public key</label>
                <textarea
                  id="cloud-key"
                  required
                  rows={3}
                  value={ssh}
                  onChange={(event) => setSSH(event.target.value)}
                />
                <small>
                  Public OpenSSH key only. Private keys stay in the operator
                  environment.
                </small>
              </div>
              {input(
                "Private credential profile ID",
                "cloud-profile",
                profile,
                setProfile,
                false,
              )}
              <button
                type="button"
                className="secondary"
                onClick={() => setEditingCredential(true)}
              >
                Configure private credentials
              </button>
              {input(
                "Application health path",
                "cloud-health",
                health,
                setHealth,
              )}
              <div className="field">
                <label htmlFor="cloud-port">Container port</label>
                <input
                  id="cloud-port"
                  type="number"
                  min={1024}
                  max={65535}
                  value={port}
                  onChange={(event) => setPort(Number(event.target.value))}
                />
              </div>
              <p>
                Required operator credentials:{" "}
                {selected?.credential_names.join(", ") || "Not reported"}.
                Configure their values privately on the factory server.
              </p>
            </>
          )}
          {step === 2 && rendered && (
            <>
              <dl className="review-grid">
                <div>
                  <dt>Tool</dt>
                  <dd>
                    {selected?.tool} · template {selected?.template_version}{" "}
                    {selected?.provider_version}
                  </dd>
                </div>
                <div>
                  <dt>Validation</dt>
                  <dd>
                    Rendered only · infrastructure not applied · cloud account
                    not verified
                  </dd>
                </div>
                <div>
                  <dt>Content digest</dt>
                  <dd>
                    <code>{rendered.content_digest}</code>
                  </dd>
                </div>
              </dl>
              <h3>Requirements</h3>
              <ul>
                {rendered.requirements.map((value) => (
                  <li key={value}>{value}</li>
                ))}
              </ul>
              <h3>Rendered files</h3>
              {Object.entries(rendered.files).map(([file, content]) => (
                <details key={file}>
                  <summary>{file}</summary>
                  <pre>{content}</pre>
                </details>
              ))}
            </>
          )}
          {step === 3 && (
            <>
              {input("Deployment target ID", "cloud-target", target, setTarget)}
              <p>
                Project:{" "}
                {repositories.find((repo) => repo.id === project)?.name} ·
                Provider: {selected?.name} · Image: <code>{image}</code>
              </p>
              <p>
                Cloud account permissions and provisioning remain unverified
                until an explicit deployment records evidence.
              </p>
            </>
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="notice" role="status">
              {notice}
            </div>
          )}
          <div className="wizard-actions">
            {step > 0 && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => {
                  setStep(step - 1);
                  setSaved(null);
                }}
              >
                Back
              </button>
            )}
            <button disabled={busy || Boolean(notice)}>
              {busy
                ? "Processing…"
                : step === 1
                  ? "Render recipe"
                  : step === 3
                    ? "Save recipe and target"
                    : "Continue"}
            </button>
            {saved && (
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void download()}
              >
                Download recipe ZIP
              </button>
            )}
          </div>
        </form>
      </Wizard>
    </div>
  );
}
