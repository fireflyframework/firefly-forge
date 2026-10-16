/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type RefObject,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Plus,
  X,
  MessageSquare,
  FileText,
  Palette,
  PanelsTopLeft,
} from "lucide-react";
import {
  post,
  request,
  type Connection,
  type AIProposalProvenance,
  type CustomerProductDefinition,
  type ProductArchitecture,
  type ProductProcess,
  type FunctionalFeature,
  type ProductBrand,
  type ProductIntegration,
  type ProductSnapshot,
  type ProjectSnapshot,
  type ProductDesignReview,
  type Repository,
} from "./api";
import {
  ProductDraftBook,
  type ProductDraftSession,
  useProductDraftField,
  useProductDraftSession,
} from "./ProductDrafts";
import { repositoryLabels } from "./RepositoryLabels";
import { TextFileInput, validateSvgText } from "./TextFileInput";
import {
  ProductDesignSystem,
  normalizeProductDesign,
  validateProductDesign,
} from "./ProductDesignSystem";
import { FunctionalFeatures, featureReadiness } from "./FunctionalFeatures";
import {
  ProductIntegrations,
  integrationReadiness,
} from "./ProductIntegrations";
import { ProcessEditor } from "./ProcessEditor";
import { buildStepPrompt } from "./ProductStepContext";
import { ProductAssistant } from "./ProductAssistant";
import { DesignReview } from "./DesignReview";
import { Wizard } from "./Wizard";
import { ProductWorkspace } from "./ProductWorkspace";
import {
  ProductOrigin,
  ProductProposal,
  type DraftProposal,
} from "./ProductProposal";
import { MarkdownEditor } from "./RichMarkdown";
import { ProductList, ProductJourneys } from "./ProductFields";
import "./ProductWorkspace.css";
import { safeExternalLink } from "./FeatureLinks";
import "./StudioFeatures.css";
const lines = (value: string) =>
  value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
const message = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The product definition could not be saved.";
const field = (
  label: string,
  id: string,
  children: ReactNode,
  hint?: string,
) => (
  <div className="field">
    <label htmlFor={id}>{label}</label>
    {children}
    {hint && <small>{hint}</small>}
  </div>
);
type Proposal = {
  method: string;
  recommendation: {
    backend: string;
    frontend: string;
    rationale: string;
    alternatives: {
      id: string;
      name: string;
      detail: string;
      source: string;
    }[];
  };
  architecture: ProductArchitecture;
};
const defaultBrand = {
  name: "",
  font_family: "Manrope" as const,
  primary: "#ffb347",
  background: "#ffffff",
  text: "#292a25",
  dark_background: "#252620",
  dark_text: "#f7f6f2",
};

export function ProductDefinition({
  connection,
  connected,
  repositories,
  initialRepository,
  logicalProject,
  onConfigure,
  onStart,
  drafts,
  onRepositoryChange,
}: {
  drafts?: ProductDraftBook;
  onRepositoryChange?: (repositoryId: string) => void;
  connection: Connection;
  connected: boolean;
  repositories: Repository[];
  initialRepository?: string;
  logicalProject?: ProjectSnapshot | null;
  onConfigure: () => void;
  onStart: (repositoryId: string) => void;
}) {
  const labels = repositoryLabels(repositories);
  const [project, setProject] = useState(initialRepository ?? "");
  const selected =
    repositories.find((repo) => repo.id === project) ?? repositories[0] ?? null;
  const selectedSource =
    selected && labels.get(selected.id) !== selected.name
      ? labels.get(selected.id)?.slice(selected.name.length + 3)
      : null;
  const [fallbackDrafts] = useState(() => new ProductDraftBook());
  const book = drafts ?? fallbackDrafts;
  const id = selected?.id;
  const session = useProductDraftSession(book, id ?? "");
  const [discarding, setDiscarding] = useState(false);
  const [snapshot, setSnapshot] = useState<ProductSnapshot | null>(
    () => session.base,
  );
  const [brand, setBrand] = useState<ProductBrand | null>(() => session.brand);
  const [loaded, setLoaded] = useState(session.observed ? (id ?? "") : "");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const revision = 0;
  const [draftSource] = useProductDraftField<AIProposalProvenance | null>(
    session,
    "draftSource",
    null,
  );
  const [dirty, setDirty] = useProductDraftField(session, "dirty", false);
  const [savedDraft] = useProductDraftField(session, "savedDraft", false);
  const [saving] = useProductDraftField(session, "busy", false);
  const [reveal, setReveal] = useState(0);
  const [sourceTarget, setSourceTarget] = useState<{
    conversation_id: string;
    turn_id: string;
  }>();
  const [pendingProposal, setPendingProposal] = useState<DraftProposal | null>(
    null,
  );
  const [stepPrompt, setStepPrompt] = useState<{ id: number; text: string }>();
  const promptCount = useRef(0);
  const reader = useRef<(() => CustomerProductDefinition) | null>(null);
  const [draft] = useProductDraftField<CustomerProductDefinition | null>(
    session,
    "draft",
    null,
  );
  const [editorKey, setEditorKey] = useState(0);
  const currentProject = useRef(id);
  currentProject.current = id;
  useEffect(() => {
    if (!connected || !id) return;
    const controller = new AbortController();
    const observationEpoch = session.observationEpoch;
    setSnapshot(session.base);
    setBrand(session.brand);
    setLoaded(session.observed ? id : "");
    setLoading(true);
    Promise.all([
      request<ProductSnapshot | null>(
        connection,
        `/repositories/${id}/product`,
        { signal: controller.signal },
      ),
      request<ProductBrand | null>(connection, `/repositories/${id}/brand`, {
        signal: controller.signal,
      }),
    ])
      .then(([product, kit]) => {
        if (!controller.signal.aborted) {
          session.observe(product, kit, observationEpoch);
          setSnapshot(session.base);
          setBrand(session.brand);
          setLoaded(id);
          setError("");
        }
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(message(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [connection, connected, id, revision, session]);
  return (
    <section className="product-definition">
      {(!connected || !selected) && (
        <div className="page-heading">
          <div>
            <h1>Product definition</h1>
            <p>
              Define the customer, journey, requirements and design before
              starting a run.
            </p>
          </div>
        </div>
      )}
      {!connected ? (
        <div className="empty">
          <h3>Connect your factory first</h3>
          <p>
            Definitions and brand kits are stored with their project on the
            server.
          </p>
        </div>
      ) : !selected ? (
        <div className="empty">
          <h3>Connect a repository first</h3>
          <p>A product definition belongs to one repository.</p>
        </div>
      ) : (
        <>
          {logicalProject &&
            selected.id ===
              (logicalProject.definition.product_repository_id ||
                logicalProject.definition.components[0].repository_id) && (
              <p className="feature-status">
                Shared product definition and design system for{" "}
                {logicalProject.definition.name} · revision{" "}
                {logicalProject.revision}
              </p>
            )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <ProductWorkspace
            header={
              <>
                <div className="page-heading">
                  <div>
                    <h1>Product definition</h1>
                    <p>
                      Define the customer, journey, requirements and design
                      before starting a run.
                    </p>
                  </div>
                </div>
                <div className="feature-toolbar product-workspace-context">
                  {field(
                    "Product project",
                    "product-project",
                    <>
                      <select
                        id="product-project"
                        value={selected.id}
                        onChange={(event) => {
                          setProject(event.target.value);
                          setStepPrompt(undefined);
                          setPendingProposal(null);
                          setDiscarding(false);
                          onRepositoryChange?.(event.target.value);
                          setSourceTarget(undefined);
                          reader.current = null;
                        }}
                      >
                        {repositories.map((repo) => (
                          <option key={repo.id} value={repo.id}>
                            {labels.get(repo.id)}
                          </option>
                        ))}
                      </select>
                      {selectedSource && (
                        <small
                          className="product-selected-source"
                          aria-label="Selected project source"
                        >
                          {selectedSource}
                        </small>
                      )}
                    </>,
                  )}
                  <ProductOrigin
                    source={
                      draftSource ??
                      (loaded === id && snapshot?.origin?.kind === "ai_assisted"
                        ? snapshot.origin
                        : null)
                    }
                    saved={
                      savedDraft || Boolean(!draft && loaded === id && snapshot)
                    }
                    dirty={dirty}
                    revision={loaded === id ? snapshot?.revision : undefined}
                    onSource={() => {
                      const origin = draftSource ?? snapshot?.origin;
                      if (origin?.conversation_id && origin.turn_id) {
                        setSourceTarget({
                          conversation_id: origin.conversation_id,
                          turn_id: origin.turn_id,
                        });
                        setReveal((value) => value + 1);
                      }
                    }}
                  />
                </div>
                {(dirty || session.stale) && (
                  <div className="product-draft-notice" role="status">
                    <span>
                      Unsaved edits stay in this tab while you navigate.
                      Reloading or signing out clears them.
                    </span>
                    <button
                      type="button"
                      className="text-button"
                      disabled={saving}
                      onClick={() => setDiscarding(true)}
                    >
                      Discard edits
                    </button>
                  </div>
                )}
                {session.stale && (
                  <div className="alert" role="alert">
                    The saved definition changed since this draft was opened.
                    Your edits and original revision are retained. Saving is
                    held. Discard this draft only when you are ready to load the
                    current saved version.
                  </div>
                )}
              </>
            }
            reveal={reveal}
            dismiss={editorKey}
            document={
              loading || loaded !== id || !session.observed ? (
                <div className="loading" role="status">
                  Loading the product definition…
                </div>
              ) : (
                <DefinitionEditor
                  key={`${id}-${session.id}`}
                  session={session}
                  connection={connection}
                  repository={selected}
                  snapshot={snapshot}
                  draft={draft}
                  initialBrand={brand}
                  reader={reader}
                  onAsk={(step, definition) => {
                    if (currentProject.current !== selected.id) return;
                    setStepPrompt({
                      id: ++promptCount.current,
                      text: buildStepPrompt(step, definition),
                    });
                    setReveal((value) => value + 1);
                  }}
                  onDirty={() => {
                    setDirty(true);
                  }}
                  onSaved={(saved) => {
                    if (
                      session.active &&
                      currentProject.current === saved.repository_id
                    ) {
                      setSnapshot(saved);
                    }
                  }}
                  onStart={() => onStart(selected.id)}
                />
              )
            }
            assistant={(close) => (
              <ProductAssistant
                onCollapse={close}
                draftPrompt={stepPrompt}
                key={selected.id}
                connection={connection}
                repository={selected.id}
                projectId={
                  logicalProject &&
                  selected.id ===
                    (logicalProject.definition.product_repository_id ||
                      logicalProject.definition.components[0].repository_id)
                    ? logicalProject.id
                    : undefined
                }
                sourceTarget={sourceTarget}
                onConfigure={onConfigure}
                onUse={(definition, source) => {
                  if (currentProject.current !== selected.id) return;
                  setPendingProposal({
                    definition,
                    source,
                    current: reader.current?.() ?? snapshot?.definition ?? null,
                  });
                }}
              />
            )}
          />
          {discarding && (
            <DiscardProductDraft
              repository={selected.name}
              onCancel={() => setDiscarding(false)}
              onDiscard={() => {
                book.discard(session);
                setDiscarding(false);
                setPendingProposal(null);
                setStepPrompt(undefined);
                reader.current = null;
                setEditorKey((value) => value + 1);
              }}
            />
          )}
          {pendingProposal && (
            <ProductProposal
              value={pendingProposal}
              onCancel={() => setPendingProposal(null)}
              onApply={() => {
                const { definition, source } = pendingProposal;
                book.apply(session, definition, source);
                setEditorKey((value) => value + 1);
                setPendingProposal(null);
              }}
            />
          )}
        </>
      )}
    </section>
  );
}

function DiscardProductDraft({
  repository,
  onCancel,
  onDiscard,
}: {
  repository: string;
  onCancel: () => void;
  onDiscard: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="feature-dialog assistant-recovery-dialog"
      aria-label="Discard product edits"
      onCancel={onCancel}
    >
      <header>
        <h2>Discard edits for {repository}?</h2>
        <button
          className="quiet"
          aria-label="Keep product edits"
          onClick={onCancel}
        >
          <X size={20} />
        </button>
      </header>
      <div className="assistant-recovery-body">
        <p>
          Your unsaved edits and current step for this product will be cleared.
          The saved definition and other products will stay unchanged.
        </p>
      </div>
      <footer>
        <button className="quiet" onClick={onCancel}>
          Keep editing
        </button>
        <button className="primary" onClick={onDiscard}>
          Discard this draft
        </button>
      </footer>
    </dialog>
  );
}

function DefinitionEditor({
  session,
  connection,
  repository,
  snapshot,
  draft,
  initialBrand,
  onSaved,
  onStart,
  reader,
  onDirty,
  onAsk,
}: {
  session: ProductDraftSession;
  onAsk: (step: number, definition: CustomerProductDefinition) => void;
  reader: RefObject<(() => CustomerProductDefinition) | null>;
  onDirty: () => void;
  connection: Connection;
  repository: Repository;
  snapshot: ProductSnapshot | null;
  draft: CustomerProductDefinition | null;
  initialBrand: ProductBrand | null;
  onSaved: (snapshot: ProductSnapshot) => void;
  onStart: () => void;
}) {
  const definition = draft ?? snapshot?.definition;
  const [step, setStep] = useProductDraftField(session, "step", 0, false);
  const [customer, setCustomer] = useProductDraftField(
    session,
    "customer",
    definition?.customer ?? "",
    true,
  );
  const [problem, setProblem] = useProductDraftField(
    session,
    "problem",
    definition?.problem ?? "",
    true,
  );
  const [journey, setJourney] = useProductDraftField(
    session,
    "journey",
    definition?.journey.join("\n") ?? "",
    true,
  );
  const [stories, setStories] = useProductDraftField(
    session,
    "stories",
    definition?.user_stories.join("\n") ?? "",
    true,
  );
  const [acceptance, setAcceptance] = useProductDraftField(
    session,
    "acceptance",
    definition?.acceptance.join("\n") ?? "",
    true,
  );
  const [security, setSecurity] = useProductDraftField(
    session,
    "security",
    definition?.security_requirements.join("\n") ?? "",
    true,
  );
  const [nonfunctional, setNonfunctional] = useProductDraftField(
    session,
    "nonfunctional",
    definition?.nonfunctional_requirements.join("\n") ?? "",
    true,
  );
  const [owners, setOwners] = useProductDraftField(
    session,
    "owners",
    definition?.owners.join("\n") ?? "",
    true,
  );
  const [architecture, setArchitecture] =
    useProductDraftField<ProductArchitecture | null>(
      session,
      "architecture",
      definition?.architecture ?? null,
      true,
    );
  const [designReview, setDesignReview] =
    useProductDraftField<ProductDesignReview | null>(
      session,
      "designReview",
      definition?.design_review ?? null,
      true,
    );
  const [processes, setProcesses] = useProductDraftField<ProductProcess[]>(
    session,
    "processes",
    definition?.processes ?? [],
    true,
  );
  const [features, setFeatures] = useProductDraftField<FunctionalFeature[]>(
    session,
    "features",
    definition?.features ?? [],
    true,
  );
  const [integrations, setIntegrations] = useProductDraftField<
    ProductIntegration[]
  >(session, "integrations", definition?.integrations ?? [], true);
  const [reviewArea, setReviewArea] = useProductDraftField<
    "overview" | "design" | "references"
  >(session, "reviewArea", "overview", false);
  const [functionalReviewed, setFunctionalReviewed] = useProductDraftField(
    session,
    "functionalReviewed",
    definition?.functional_reviewed ?? false,
    true,
  );
  const [visualDesign, setVisualDesign] = useProductDraftField(
    session,
    "visualDesign",
    normalizeProductDesign(definition?.visual_design),
    true,
  );
  const [designPending, setDesignPending] = useProductDraftField(
    session,
    "designPending",
    false,
    false,
  );
  const [designGeneration, setDesignGeneration] = useProductDraftField(
    session,
    "designGeneration",
    0,
    false,
  );
  const invalidateDesign = () => {
    setDesignReview(null);
    setDesignPending(false);
    setDesignGeneration((value) => value + 1);
  };
  const updateArchitecture = (
    value:
      | ProductArchitecture
      | null
      | ((current: ProductArchitecture | null) => ProductArchitecture | null),
  ) => {
    setArchitecture(value);
    invalidateDesign();
  };
  const [proposal, setProposal] = useProductDraftField<Proposal | null>(
    session,
    "proposal",
    null,
    false,
  );
  const [backend, setBackend] = useProductDraftField(
    session,
    "backend",
    architecture?.backend ?? "pyfly",
    true,
  );
  const [frontend, setFrontend] = useProductDraftField(
    session,
    "frontend",
    architecture?.frontend ?? "react",
    true,
  );
  const [brand, setBrand] = useProductDraftField<ProductBrand | null>(
    session,
    "brand",
    initialBrand,
    false,
  );
  const [useBrand, setUseBrand] = useProductDraftField(
    session,
    "useBrand",
    Boolean(initialBrand),
    true,
  );
  const [palette, setPalette] = useProductDraftField(
    session,
    "palette",
    {
      ...defaultBrand,
      name: initialBrand?.name ?? repository.name,
      ...Object.fromEntries(
        [
          ["primary", "light", "primary"],
          ["background", "light", "background"],
          ["text", "light", "text"],
          ["dark_background", "dark", "background"],
          ["dark_text", "dark", "text"],
        ].flatMap(([field, mode, name]) => {
          const token = (
            initialBrand?.tokens.color as
              | Record<string, Record<string, { $value?: { hex?: string } }>>
              | undefined
          )?.[mode]?.[name]?.$value?.hex;
          return token ? [[field, token]] : [];
        }),
      ),
    },
    true,
  );
  const [tokenImport, setTokenImport] = useProductDraftField(
    session,
    "tokenImport",
    initialBrand ? JSON.stringify(initialBrand.tokens, null, 2) : "",
    true,
  );
  const [logoImport, setLogoImport] = useProductDraftField(
    session,
    "logoImport",
    initialBrand?.files["logo.svg"] ?? "",
    true,
  );
  const [darkLogoImport, setDarkLogoImport] = useProductDraftField(
    session,
    "darkLogoImport",
    initialBrand?.files["logo-dark.svg"] ?? "",
    true,
  );
  const [brandChanged, setBrandChanged] = useProductDraftField(
    session,
    "brandChanged",
    false,
    true,
  );
  const [busy, setBusy] = useProductDraftField(session, "busy", false);
  const [error, setError] = useProductDraftField(session, "error", "");
  const [saved, setSaved] = useProductDraftField(session, "saved", false);
  const fullyReviewed = Boolean(
    functionalReviewed &&
    (!architecture || architecture.reviewed) &&
    designReview,
  );
  const missingReviews = (value: CustomerProductDefinition) =>
    [
      !value.functional_reviewed ? "functional behavior" : "",
      value.architecture && !value.architecture.reviewed ? "architecture" : "",
      !value.design_review ? "reference views" : "",
    ].filter(Boolean);
  const savedMissingReviews = snapshot
    ? missingReviews(snapshot.definition)
    : [];
  const data = (): CustomerProductDefinition => ({
    ai_origin: definition?.ai_origin ?? null,
    customer: customer.trim(),
    problem: problem.trim(),
    journey: lines(journey),
    user_stories: lines(stories),
    acceptance: lines(acceptance),
    nonfunctional_requirements: lines(nonfunctional),
    security_requirements: lines(security),
    owners: lines(owners),
    design_review: designReview,
    processes,
    features,
    integrations: structuredClone(integrations),
    functional_reviewed: functionalReviewed,
    visual_design: visualDesign,
    architecture: architecture
      ? { ...architecture, nonfunctional_requirements: lines(nonfunctional) }
      : null,
  });
  useEffect(() => {
    reader.current = data;
  });
  async function propose() {
    setBusy(true);
    setError("");
    try {
      const input = data();
      if (!input.acceptance.length)
        throw new Error(
          "Enter acceptance criteria before proposing an architecture.",
        );
      const result = await post<Proposal>(connection, "/architecture/propose", {
        product_definition: { ...input, architecture: null },
        backend,
        frontend,
      });
      setProposal(result);
      setArchitecture(result.architecture);
      invalidateDesign();
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function saveDesignSettings() {
    setBusy(true);
    setError("");
    try {
      validateProductDesign(visualDesign);
      if (useBrand && (!brand || brandChanged)) {
        const tokens: unknown = tokenImport.trim()
          ? JSON.parse(tokenImport)
          : undefined;
        const kit = await request<ProductBrand>(
          connection,
          `/repositories/${repository.id}/brand`,
          {
            method: "PUT",
            body: JSON.stringify({
              ...palette,
              ...(tokens === undefined ? {} : { tokens }),
              ...(logoImport.trim() ? { logo_svg: logoImport } : {}),
              ...(darkLogoImport.trim()
                ? { logo_dark_svg: darkLogoImport }
                : {}),
            }),
          },
        );
        setBrand(kit);
        invalidateDesign();
        setBrandChanged(false);
      }
      setReviewArea("overview");
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      const input = data();
      if (
        step === 0 &&
        (!input.customer || !input.problem || !input.owners.length)
      )
        throw new Error("Enter the customer, problem and at least one owner.");
      if (step === 1 && (!input.journey.length || !input.user_stories.length))
        throw new Error("Add a journey and at least one user story.");
      if (step === 2 && !input.acceptance.length)
        throw new Error("Add observable acceptance criteria.");
      if (
        step === 2 &&
        features.length &&
        !featureReadiness(features, input.journey, input.acceptance)
      )
        throw new Error(
          "Complete feature names, journey and acceptance links, Given/When/Then behavior and failure paths before continuing.",
        );
      if (step === 3) {
        const invalid = integrationReadiness(
          integrations,
          input.journey,
          features,
        );
        if (invalid) throw new Error(invalid);
      }
      if (step === 4) {
        validateProductDesign(visualDesign);
        if (useBrand && (!brand || brandChanged))
          throw new Error(
            "Save your design settings before saving the definition.",
          );
      }
      if (step < 4) {
        setStep((value) => value + 1);
        return;
      }
      if (designPending)
        throw new Error(
          "Review every screen in both viewports and themes before saving, or remove the wireframe review.",
        );
      if (architecture && !architecture.reviewed)
        throw new Error(
          "Review the proposed architecture before saving the product definition.",
        );
      if (!session.active || session.stale)
        throw new Error(
          "The saved definition changed. Your draft is retained; saving is held.",
        );
      const submittedVersion = session.editVersion;
      const result = await request<ProductSnapshot>(
        connection,
        `/repositories/${repository.id}/product`,
        { method: "PUT", body: JSON.stringify(data()) },
      );
      if (session.acceptSaved(result, submittedVersion)) {
        setSaved(true);
        onSaved(result);
      } else if (session.active) {
        setError(
          "The earlier save finished, but the draft or observed server revision changed. Your draft is retained.",
        );
      }
    } catch (cause) {
      setError(message(cause));
    } finally {
      setBusy(false);
    }
  }
  const edit = (setter: (value: string) => void, value: string) => {
    setter(value);
    setFunctionalReviewed(false);
    setSaved(false);
    invalidateDesign();
    if (architecture) updateArchitecture({ ...architecture, reviewed: false });
  };
  const text = (
    label: string,
    id: string,
    value: string,
    setter: (value: string) => void,
    required = true,
    rows = 4,
    hint?: string,
  ) =>
    field(
      label,
      id,
      <textarea
        id={id}
        required={required}
        rows={rows}
        maxLength={id.startsWith("brand-") ? 65536 : 8000}
        value={value}
        onChange={(event) => {
          setter(event.target.value);
          setFunctionalReviewed(false);
          setSaved(false);
          invalidateDesign();
          if (architecture)
            updateArchitecture({ ...architecture, reviewed: false });
        }}
      />,
      hint,
    );
  return (
    <>
      <Wizard
        progressMode="visited"
        pageFlow
        headingAction={
          <button
            type="button"
            className="text-button"
            aria-label="Ask about this step"
            onClick={() => onAsk(step, data())}
          >
            <MessageSquare size={18} />
            Ask AI
          </button>
        }
        locked={busy}
        steps={["Context", "Journeys", "Features", "Integrations", "Review"]}
        active={step}
        onStep={setStep}
        title={
          [
            "Who is this product for?",
            "What does the customer need to do?",
            "What should the product do?",
            "Which systems does it use?",
            "Review before saving",
          ][step]
        }
        description={
          [
            "Name the customer, describe their problem, and choose the people responsible for this product.",
            "Put the customer’s actions in order, then describe the outcome they expect.",
            "Link each journey to features, observable outcomes, and what happens when things go wrong.",
            "Describe your services and third-party connections. No live connection is made by saving these contracts.",
            "Review the definition and any attached design. Saving a definition and implementing it are separate actions.",
          ][step]
        }
      >
        <form
          onSubmit={(event) => void submit(event)}
          onChangeCapture={onDirty}
          onFocusCapture={(event) => {
            const control = event.target as HTMLElement,
              scroll = control.closest<HTMLElement>(".wizard-field-scroll");
            if (!scroll || !control.matches("input,select,textarea,button"))
              return;
            const main = control.closest<HTMLElement>("main");
            if (!main) return;
            const target = control.getBoundingClientRect(),
              bounds = main.getBoundingClientRect();
            if (target.height > bounds.height - 32) return;
            if (target.bottom > bounds.bottom - 16)
              main.scrollTop += target.bottom - bounds.bottom + 16;
            else if (target.top < bounds.top + 16)
              main.scrollTop -= bounds.top - target.top + 16;
          }}
        >
          <div className="product-step-purpose">
            <p>
              {
                [
                  "Required: customer, problem and at least one product owner.",
                  "Required: at least one journey and a user story.",
                  "Required: acceptance outcomes. Map features to record a functional review.",
                  "Optional when this product operates independently.",
                  "Save a draft with pending reviews, or record the completed human reviews.",
                ][step]
              }
            </p>
          </div>
          {step === 0 && (
            <div className="product-brief-grid">
              <div className="product-customer-summary">
                {field(
                  "Customer or user",
                  "product-customer",
                  <input
                    id="product-customer"
                    value={customer}
                    required
                    maxLength={4000}
                    onChange={(event) => edit(setCustomer, event.target.value)}
                  />,
                  "Who will use this product?",
                )}
              </div>
              <div className="product-customer-narrative">
                <MarkdownEditor
                  id="product-problem"
                  label="Problem to solve"
                  value={problem}
                  required
                  onChange={(value) => edit(setProblem, value)}
                  hint="Required · Describe the problem and why it matters."
                />
              </div>
              <div className="product-customer-facts">
                <ProductList
                  label="Product owners"
                  id="product-owners"
                  value={owners}
                  onChange={(value) => edit(setOwners, value)}
                  hint="Accountable for acceptance."
                  maximum={30}
                  hintAfter
                  compact
                />
              </div>
            </div>
          )}
          {step === 1 && (
            <>
              <ProductJourneys
                value={journey}
                onChange={(value) => edit(setJourney, value)}
                customer={customer}
                features={features}
              />
              <ProductList
                label="User stories"
                id="product-stories"
                value={stories}
                onChange={(value) => edit(setStories, value)}
                hint="Required · Describe the person, goal and expected benefit."
                maximum={100}
              />
            </>
          )}
          {step === 2 && (
            <>
              <ProductList
                label="Acceptance criteria"
                id="product-acceptance"
                value={acceptance}
                onChange={(value) => edit(setAcceptance, value)}
                hint="Required · One observable outcome per item; features link to these outcomes."
                maximum={100}
              />
              <FunctionalFeatures
                value={features}
                journeys={lines(journey)}
                acceptance={lines(acceptance)}
                onChange={(value) => {
                  setFeatures(value);
                  setFunctionalReviewed(false);
                  setSaved(false);
                  invalidateDesign();
                }}
              />
              <ProductList
                label="Security requirements"
                id="product-security"
                value={security}
                onChange={(value) => edit(setSecurity, value)}
                hint="Add authentication, authorization and data-handling requirements."
                required={false}
              />
              <ProductList
                label="Nonfunctional requirements"
                id="product-nonfunctional"
                value={nonfunctional}
                onChange={(value) => edit(setNonfunctional, value)}
                hint="Add measurable performance, reliability and accessibility requirements."
                required={false}
              />
              <ProcessEditor
                value={processes}
                journeys={lines(journey)}
                acceptance={lines(acceptance)}
                onChange={(value) => {
                  setProcesses(value);
                  setSaved(false);
                  invalidateDesign();
                }}
              />
              <section className="integration-section">
                <h3>Architecture proposal</h3>
                <div className="two-columns">
                  {field(
                    "Preferred backend",
                    "product-backend",
                    <select
                      id="product-backend"
                      value={backend}
                      onChange={(event) => setBackend(event.target.value)}
                    >
                      <option value="pyfly">PyFly · Python</option>
                      <option value="larafly">LaraFly · Laravel</option>
                      <optgroup label="Additional profiles — review validation limits">
                        <option value="java">Firefly · Java</option>
                        <option value="rust">Rust</option>
                        <option value="dotnet">.NET</option>
                      </optgroup>
                    </select>,
                  )}
                  {field(
                    "Preferred frontend",
                    "product-frontend",
                    <select
                      id="product-frontend"
                      value={frontend}
                      onChange={(event) => setFrontend(event.target.value)}
                    >
                      {["react", "angular", "nextjs", "vue", "none"].map(
                        (id) => (
                          <option value={id} key={id}>
                            {id === "none"
                              ? "API / microservice · no frontend"
                              : id}
                          </option>
                        ),
                      )}
                    </select>,
                  )}
                </div>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || !lines(acceptance).length}
                  onClick={() => void propose()}
                >
                  Propose architecture
                </button>
                {proposal && (
                  <div className="feature-record">
                    <h3>Rule-based recommendation</h3>
                    <p>{proposal.recommendation.rationale}</p>
                    <details>
                      <summary>Alternatives and source references</summary>
                      {proposal.recommendation.alternatives.map((item) => (
                        <article key={item.id}>
                          <h4>{item.name}</h4>
                          <p>{item.detail}</p>
                          {safeExternalLink(item.source) && (
                            <a
                              href={safeExternalLink(item.source)!}
                              target="_blank"
                              rel="noreferrer noopener"
                            >
                              Read source
                            </a>
                          )}
                        </article>
                      ))}
                    </details>
                  </div>
                )}
                {architecture && (
                  <div className="architecture-components">
                    <p className="integration-status">
                      Proposed stack: {architecture.backend} /{" "}
                      {architecture.frontend}. Review each responsibility before
                      saving.
                    </p>
                    {architecture.components.map((component, index) => (
                      <article className="feature-record" key={index}>
                        <div className="two-columns">
                          {field(
                            "Component name",
                            `component-name-${index}`,
                            <input
                              id={`component-name-${index}`}
                              required
                              value={component.name}
                              onChange={(event) =>
                                updateArchitecture(
                                  (current) =>
                                    current && {
                                      ...current,
                                      reviewed: false,
                                      components: current.components.map(
                                        (item, itemIndex) =>
                                          itemIndex === index
                                            ? {
                                                ...item,
                                                name: event.target.value,
                                              }
                                            : item,
                                      ),
                                    },
                                )
                              }
                            />,
                          )}
                          {field(
                            "Component layer",
                            `component-layer-${index}`,
                            <select
                              id={`component-layer-${index}`}
                              value={component.layer}
                              onChange={(event) =>
                                updateArchitecture(
                                  (current) =>
                                    current && {
                                      ...current,
                                      reviewed: false,
                                      components: current.components.map(
                                        (item, itemIndex) =>
                                          itemIndex === index
                                            ? {
                                                ...item,
                                                layer: event.target
                                                  .value as typeof component.layer,
                                              }
                                            : item,
                                      ),
                                    },
                                )
                              }
                            >
                              {[
                                "domain",
                                "application",
                                "data",
                                "web",
                                "frontend",
                                "operations",
                              ].map((layer) => (
                                <option key={layer} value={layer}>
                                  {layer}
                                </option>
                              ))}
                            </select>,
                          )}
                        </div>
                        {field(
                          "Responsibility",
                          `component-responsibility-${index}`,
                          <textarea
                            id={`component-responsibility-${index}`}
                            required
                            value={component.responsibility}
                            onChange={(event) =>
                              updateArchitecture(
                                (current) =>
                                  current && {
                                    ...current,
                                    reviewed: false,
                                    components: current.components.map(
                                      (item, itemIndex) =>
                                        itemIndex === index
                                          ? {
                                              ...item,
                                              responsibility:
                                                event.target.value,
                                            }
                                          : item,
                                    ),
                                  },
                              )
                            }
                          />,
                        )}
                        <button
                          type="button"
                          className="quiet"
                          onClick={() =>
                            updateArchitecture(
                              (current) =>
                                current && {
                                  ...current,
                                  reviewed: false,
                                  components: current.components.filter(
                                    (_, itemIndex) => itemIndex !== index,
                                  ),
                                },
                            )
                          }
                        >
                          <X size={16} />
                          Remove component
                        </button>
                      </article>
                    ))}
                    <button
                      type="button"
                      className="secondary"
                      onClick={() =>
                        updateArchitecture(
                          (current) =>
                            current && {
                              ...current,
                              reviewed: false,
                              components: [
                                ...current.components,
                                {
                                  name: "",
                                  layer: "application",
                                  responsibility: "",
                                },
                              ],
                            },
                        )
                      }
                    >
                      <Plus size={16} />
                      Add component
                    </button>
                    {text(
                      "Architecture decisions",
                      "architecture-decisions",
                      architecture.decisions.join("\n"),
                      (value) =>
                        updateArchitecture(
                          (current) =>
                            current && {
                              ...current,
                              reviewed: false,
                              decisions: lines(value),
                            },
                        ),
                      false,
                    )}
                    <button
                      type="button"
                      className="text-button"
                      onClick={() => updateArchitecture(null)}
                    >
                      Continue without an architecture proposal
                    </button>
                  </div>
                )}
              </section>
            </>
          )}
          {step === 3 && (
            <ProductIntegrations
              value={integrations}
              journeys={lines(journey)}
              features={features}
              onChange={(value) => {
                setIntegrations(value);
                setFunctionalReviewed(false);
                setSaved(false);
                invalidateDesign();
              }}
            />
          )}
          {step === 4 && (
            <div
              className="feature-tabs product-review-areas"
              role="group"
              aria-label="Product review areas"
            >
              {(
                [
                  ["overview", "Definition", FileText],
                  ["design", "Design settings", Palette],
                  ["references", "Reference views", PanelsTopLeft],
                ] as const
              ).map(([id, label, Icon]) => (
                <button
                  type="button"
                  aria-pressed={reviewArea === id}
                  key={id}
                  onClick={() => setReviewArea(id)}
                >
                  <Icon size={18} />
                  {label}
                </button>
              ))}
            </div>
          )}
          {step === 4 && reviewArea === "design" && (
            <>
              <label className="check-choice">
                <input
                  type="checkbox"
                  checked={useBrand}
                  onChange={(event) => setUseBrand(event.target.checked)}
                />
                Configure a product brand kit
              </label>
              {!useBrand && (
                <p className="integration-status">
                  {brand
                    ? "The saved kit remains available and will be frozen for future runs."
                    : "No product kit is configured. Runs can still use the saved customer definition."}
                </p>
              )}
              {useBrand && (
                <>
                  <div className="field">
                    <label htmlFor="brand-name">Product brand name</label>
                    <input
                      id="brand-name"
                      required
                      value={palette.name}
                      onChange={(event) => {
                        setPalette((current) => ({
                          ...current,
                          name: event.target.value,
                        }));
                        setBrandChanged(true);
                      }}
                    />
                  </div>
                  <p className="integration-status">
                    Typography: Manrope. The kit supplies atomic UI tokens,
                    themes and a product wordmark. Editing palette colors
                    generates a new token kit; imported tokens are retained
                    until you edit the palette.
                  </p>
                  <div className="product-palette">
                    {(
                      [
                        "primary",
                        "background",
                        "text",
                        "dark_background",
                        "dark_text",
                      ] as const
                    ).map((key) =>
                      field(
                        key.replaceAll("_", " "),
                        `brand-${key}`,
                        <input
                          id={`brand-${key}`}
                          key={key}
                          type="color"
                          value={palette[key]}
                          onChange={(event) => {
                            setPalette((current) => ({
                              ...current,
                              [key]: event.target.value,
                            }));
                            setTokenImport("");
                            setBrandChanged(true);
                          }}
                        />,
                      ),
                    )}
                  </div>
                  <details>
                    <summary>
                      Import supported tokens or product artwork
                    </summary>
                    <TextFileInput
                      label="Upload design tokens JSON"
                      extensions={[".json"]}
                      maxBytes={65536}
                      onRead={(value) => {
                        const tokens: unknown = JSON.parse(value);
                        if (
                          !tokens ||
                          typeof tokens !== "object" ||
                          Array.isArray(tokens)
                        )
                          throw new Error("Choose a JSON token object.");
                        setTokenImport(value);
                        setBrandChanged(true);
                        invalidateDesign();
                      }}
                      onClear={() => {
                        setTokenImport("");
                        setBrandChanged(true);
                        invalidateDesign();
                      }}
                    />
                    <TextFileInput
                      label="Upload light logo SVG"
                      extensions={[".svg"]}
                      maxBytes={65536}
                      onRead={(value) => {
                        validateSvgText(value);
                        setLogoImport(value);
                        setBrandChanged(true);
                        invalidateDesign();
                      }}
                      onClear={() => {
                        setLogoImport("");
                        setBrandChanged(true);
                        invalidateDesign();
                      }}
                    />
                    <TextFileInput
                      label="Upload dark logo SVG"
                      extensions={[".svg"]}
                      maxBytes={65536}
                      onRead={(value) => {
                        validateSvgText(value);
                        setDarkLogoImport(value);
                        setBrandChanged(true);
                        invalidateDesign();
                      }}
                      onClear={() => {
                        setDarkLogoImport("");
                        setBrandChanged(true);
                        invalidateDesign();
                      }}
                    />
                    {text(
                      "DTCG tokens JSON",
                      "brand-tokens",
                      tokenImport,
                      (value) => {
                        setTokenImport(value);
                        setBrandChanged(true);
                      },
                      false,
                      8,
                      "Paste supported DTCG JSON. The server validates explicit token types. Resolve token aliases before importing.",
                    )}
                    {text(
                      "Product logo SVG",
                      "brand-logo",
                      logoImport,
                      (value) => {
                        setLogoImport(value);
                        setBrandChanged(true);
                      },
                      false,
                      6,
                      "Optional safe SVG. External references, scripts and embedded HTML are rejected.",
                    )}
                    {text(
                      "Dark product logo SVG",
                      "brand-dark-logo",
                      darkLogoImport,
                      (value) => {
                        setDarkLogoImport(value);
                        setBrandChanged(true);
                      },
                      false,
                      6,
                      "Optional dark variant. The server validates safe vector content.",
                    )}
                  </details>
                  {brand && (
                    <div className="feature-record">
                      <h3>Saved brand kit</h3>
                      <p>Revision {brand.revision}</p>
                      {brand.files["logo.svg"] && (
                        <img
                          className="product-logo-preview"
                          src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(brand.files["logo.svg"])}`}
                          alt={`${brand.name} product wordmark`}
                        />
                      )}
                      <details>
                        <summary>Generated files</summary>
                        {Object.entries(brand.files).map(([name, contents]) => (
                          <details key={name}>
                            <summary>{name}</summary>
                            <pre>{contents}</pre>
                          </details>
                        ))}
                      </details>
                    </div>
                  )}
                </>
              )}
            </>
          )}
          {step === 4 && reviewArea === "design" && (
            <ProductDesignSystem
              value={visualDesign}
              brand={brand}
              onChange={(value) => {
                setVisualDesign(value);
                setSaved(false);
                invalidateDesign();
              }}
            />
          )}
          {step === 4 && reviewArea === "design" && (
            <div className="product-design-actions">
              <button
                type="button"
                className="secondary"
                disabled={busy}
                onClick={() => void saveDesignSettings()}
              >
                Save design settings
              </button>
              <p>
                The brand is saved now. Layout settings are included when you
                save this definition.
              </p>
            </div>
          )}
          {step === 4 && reviewArea === "overview" && (
            <>
              <dl className="wizard-summary">
                <div>
                  <dt>Customer</dt>
                  <dd>{customer}</dd>
                </div>
                <div>
                  <dt>Problem</dt>
                  <dd>{problem}</dd>
                </div>
                <div>
                  <dt>Journey</dt>
                  <dd>
                    {lines(journey).map((line) => (
                      <p key={line}>{line}</p>
                    ))}
                  </dd>
                </div>
                <div>
                  <dt>Acceptance</dt>
                  <dd>
                    {lines(acceptance).map((line) => (
                      <p key={line}>{line}</p>
                    ))}
                  </dd>
                </div>
                <div>
                  <dt>Features</dt>
                  <dd>
                    {features.length
                      ? features.map((feature) => (
                          <p key={feature.id}>{feature.name}</p>
                        ))
                      : "No features mapped · draft"}
                  </dd>
                </div>
                <div>
                  <dt>Integrations</dt>
                  <dd>
                    {integrations.length
                      ? integrations.map((item) => (
                          <p key={item.id}>
                            {item.name} ·{" "}
                            {item.kind === "owned"
                              ? "Your service"
                              : "Third-party service"}
                          </p>
                        ))
                      : "No system contracts attached"}
                  </dd>
                </div>
                <div>
                  <dt>Owners</dt>
                  <dd>{lines(owners).join(", ")}</dd>
                </div>
                <div>
                  <dt>Architecture</dt>
                  <dd>
                    {architecture
                      ? `${architecture.backend} / ${architecture.frontend}, ${architecture.components.length} components`
                      : "No architecture proposal attached"}
                  </dd>
                </div>
                <div>
                  <dt>Brand</dt>
                  <dd>
                    {brand
                      ? `${brand.name}, saved revision ${brand.revision}`
                      : "No saved kit · optional"}
                  </dd>
                </div>
              </dl>
              <section
                className="product-review-status"
                aria-label="Definition review status"
              >
                <h3>Review status</h3>
                <dl>
                  <div>
                    <dt>Functional behavior</dt>
                    <dd>
                      {functionalReviewed
                        ? "Human review recorded"
                        : "Pending review"}
                    </dd>
                  </div>
                  <div>
                    <dt>Architecture</dt>
                    <dd>
                      {architecture
                        ? architecture.reviewed
                          ? "Human review recorded"
                          : "Pending review"
                        : "Not attached · optional"}
                    </dd>
                  </div>
                  <div>
                    <dt>Reference views</dt>
                    <dd>
                      {designReview
                        ? "Human reference review recorded; implementation fidelity remains unverified"
                        : "Pending · no reference review attached"}
                    </dd>
                  </div>
                </dl>
                <p>
                  Step numbers show where you have been. Saving retains the
                  current review flags; it does not approve missing reviews.
                </p>
              </section>
              <button
                type="button"
                className="secondary"
                onClick={() => setReviewArea("references")}
              >
                Review reference views
              </button>
              <section className="integration-section">
                <h3>Functional definition review</h3>
                <p>
                  {featureReadiness(features, lines(journey), lines(acceptance))
                    ? "Every journey maps to concrete features, expected behavior and failure paths."
                    : "Functional mapping is pending. This definition does not claim reviewed feature completeness."}
                </p>
                <label className="check-choice">
                  <input
                    type="checkbox"
                    disabled={
                      !featureReadiness(
                        features,
                        lines(journey),
                        lines(acceptance),
                      )
                    }
                    checked={functionalReviewed}
                    onChange={(event) => {
                      setFunctionalReviewed(event.target.checked);
                      invalidateDesign();
                    }}
                  />
                  I reviewed the mapped behaviors, failure paths and acceptance
                  links.
                </label>
              </section>
              {architecture && (
                <label className="check-choice">
                  <input
                    type="checkbox"
                    required
                    checked={architecture.reviewed}
                    onChange={(event) =>
                      updateArchitecture(
                        (current) =>
                          current && {
                            ...current,
                            reviewed: event.target.checked,
                          },
                      )
                    }
                  />
                  I reviewed the architecture layers, responsibilities and
                  decisions.
                </label>
              )}
            </>
          )}
          {step === 4 && reviewArea === "references" && (
            <DesignReview
              key={designGeneration}
              connection={connection}
              repository={repository.id}
              definition={data()}
              value={designReview}
              pending={designPending}
              onChange={setDesignReview}
              onPending={setDesignPending}
            />
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          <div className="wizard-actions">
            {saved && (
              <p role="status" className="wizard-saved-message">
                Product definition saved.
              </p>
            )}
            {step ? (
              <button
                type="button"
                className="secondary"
                onClick={() => setStep((value) => value - 1)}
              >
                <ArrowLeft size={16} />
                Back
              </button>
            ) : (
              <span>
                {snapshot
                  ? `Editing saved revision ${snapshot.revision}`
                  : "New product definition"}
              </span>
            )}
            <button
              className="primary"
              disabled={busy || (step === 4 && (saved || session.stale))}
            >
              {step === 4
                ? fullyReviewed
                  ? "Save reviewed definition"
                  : "Save draft definition"
                : step === 3
                  ? "Review definition"
                  : "Continue"}
              <ArrowRight size={16} />
            </button>
          </div>
        </form>
      </Wizard>
      {snapshot && step === 4 && (
        <div className="feature-actions">
          <button className="primary" onClick={onStart}>
            {savedMissingReviews.length
              ? "Start run from saved draft"
              : "Start run"}{" "}
            · revision {snapshot.revision}
            <ArrowRight size={16} />
          </button>
          <span className="subtle">
            {savedMissingReviews.length
              ? `Saved draft: ${savedMissingReviews.join(", ")} review is pending. The run retains this state; verification and any requested independent review still execute separately.`
              : "The saved revision and its reference reviews are frozen when the run is queued. Implementation and visual fidelity still require executable checks."}
          </span>
        </div>
      )}
    </>
  );
}
