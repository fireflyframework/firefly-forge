/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export type AssistantSourceContext = {
  attachments?: { id: string; filename: string }[];
  knowledge?: { citations?: SourceReceipt[] } | null;
  analysis_snapshot?: { citations?: SourceReceipt[] } | null;
};
type SourceReceipt = {
  id?: string;
  knowledge_id?: string;
  memory_id?: string;
  source_title?: string;
  source_filename?: string;
  filename?: string;
  title?: string;
};
export function AssistantSources({
  ids,
  context,
}: {
  ids: string[];
  context?: AssistantSourceContext;
}) {
  const names = new Map<string, string>();
  for (const file of context?.attachments ?? [])
    names.set(file.id, file.filename);
  for (const receipt of [
    ...(context?.knowledge?.citations ?? []),
    ...(context?.analysis_snapshot?.citations ?? []),
  ]) {
    const id = receipt.id ?? receipt.knowledge_id ?? receipt.memory_id;
    const name =
      receipt.source_title ??
      receipt.source_filename ??
      receipt.filename ??
      receipt.title;
    if (id && name && !names.has(id)) names.set(id, name);
  }
  const sources = ids.map((id, index) => ({
    id,
    name: names.get(id) ?? `Source ${index + 1}`,
  }));
  if (!sources.length) return null;
  return (
    <div className="assistant-citation-sources feature-meta">
      <p>Sources: {sources.map((source) => source.name).join(", ")}</p>
      <details>
        <summary>Source receipts</summary>
        <dl>
          {sources.map((source) => (
            <div key={source.id}>
              <dt>{source.name}</dt>
              <dd>{source.id}</dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}
