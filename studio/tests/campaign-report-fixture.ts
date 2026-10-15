/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Fictional candidate-backend PG exports, replayed as controlled presentation fixtures.
// They establish DTO compatibility; browser replay does not execute native campaign work.
export const verifiedReport = {
  version: 1,
  project_id: "b34c35ee67fa42c4b97f13a2711f90fc",
  campaign: {
    id: "bcd2f6d50d59427a935fae5e44aa24af",
    recorded_status: "running",
    project_digest:
      "7d662abde0a903d22d80540b20507d6107029ce7ef255e94522669f3fabd6e79",
    request_digest:
      "2b7094a6c8b1de99d34ad29ec08df960ce14b62cb4508c237e734dc627297257",
    journal_head:
      "07e4ba16f2adfd98b6da00f99e931bc8d2828f7cb7cd278da228be6dd9e27b43",
    event_count: 2,
  },
  state: "verified",
  reason: null,
  counts: {
    verified: 1,
  },
  components: [
    {
      component_id: "api",
      repository_id: "c8df8efbb7b6474a9c798b7ad9e87751",
      run_id: "428662f813a0451a9553aea3e2248be8",
      attempt: 1,
      state: "verified",
      recorded_status: "succeeded",
      reason: null,
      verification: "verified",
      admission_digest:
        "f8240cd98cd43ab80d26e7de37b2986ae2a7d4665b47226398c189bc875df513",
      run_digest:
        "360865642a2aed2887dc223706316236794a872dbe8f01a0fcde3c3cac05bfd2",
      source_base: "f593ccd8be4fa0866be8d0a70fbac57924650b36",
      branch: "forge/428662f813a0",
      branch_receipt: {
        sequence: 3,
        digest:
          "95791633ffbbbce5d1629ac9b76c2b7ab4457200c160801794a7bad49fb1ad44",
      },
      checkpoint_revision: "f593ccd8be4fa0866be8d0a70fbac57924650b36",
      source_tree: "80b8b87f220b8730d2f11fab094a2a61faa64e41",
      source_tree_receipt: {
        sequence: 7,
        digest:
          "f6d75824dc800c9a47bf887903dc5b09566dae27b38aabd64a54c5ac7dfd7d05",
      },
      checkpoint_receipt: {
        sequence: 7,
        digest:
          "f6d75824dc800c9a47bf887903dc5b09566dae27b38aabd64a54c5ac7dfd7d05",
      },
      plan_digest:
        "eb9320c789fad6dffa4b0ea1b41fae6323e941da95b173104098cf017ec407a2",
      journal: {
        sequence: 7,
        digest:
          "f6d75824dc800c9a47bf887903dc5b09566dae27b38aabd64a54c5ac7dfd7d05",
        head: "f6d75824dc800c9a47bf887903dc5b09566dae27b38aabd64a54c5ac7dfd7d05",
      },
      tasks: [
        {
          id: "checkpoint",
          attempt: 1,
          state: "receipted",
          evidence_digest:
            "5979b4223de692671c9b45df7ad0c4fbf199703dc9613d1d5cc5b9254bc842a9",
          receipt: {
            sequence: 7,
            digest:
              "f6d75824dc800c9a47bf887903dc5b09566dae27b38aabd64a54c5ac7dfd7d05",
          },
        },
        {
          id: "prepare",
          attempt: 1,
          state: "receipted",
          evidence_digest:
            "45f5b94a87a6888348c29ea730a356072d9d99099e96620ac34f188796550581",
          receipt: {
            sequence: 3,
            digest:
              "95791633ffbbbce5d1629ac9b76c2b7ab4457200c160801794a7bad49fb1ad44",
          },
        },
        {
          id: "verify",
          attempt: 1,
          state: "receipted",
          evidence_digest:
            "2f3fc16511a8354edd5743aa6886b8bfe976519e14025f0170d811e9ea208c13",
          receipt: {
            sequence: 5,
            digest:
              "22baec224162e11500bfa12ebc9f7909b0f797b55d7b291b7b77b3718362f918",
          },
        },
      ],
      checks: [
        {
          command_digest:
            "e5b98db6ae7742bd3e11b4ac98a3835e804d67b32925fd45984ca8045994e747",
          argument_count: 2,
          exit_code: 0,
          receipt: {
            sequence: 5,
            digest:
              "22baec224162e11500bfa12ebc9f7909b0f797b55d7b291b7b77b3718362f918",
          },
        },
      ],
      prior_attempts: [],
      depends_on: [],
    },
  ],
  milestones: "not_configured",
  waves: "not_configured",
  scm_freshness: "not_observed",
  limits: {
    attempts: 200,
    campaign_events: 1024,
    tasks_per_run: 128,
    journal_entries_per_run: 2048,
    run_events_per_run: 2048,
    snapshot_seconds: 10,
    evidence_json_bytes: 16777216,
  },
  digest: "09401c79a91a1dc3a528ac3ae6d786b578d5a8016325001f42dc7ef635547c39",
};
export const unknownReport = {
  version: 1,
  project_id: "a1ecaa29e9014a109b96312e998a6dca",
  campaign: {
    id: "c1b88ed9ff3a4431b2b17517108a8b0a",
    recorded_status: "running",
    project_digest:
      "0a3b7f0e5b6a94a40b81949b8ed4e570098a3b13839d6dffe9b8472a9d07d74f",
    request_digest:
      "6881647fb93516e28da324db8600da644f8c5354e4411ed8af911fe2e7d02e7f",
    journal_head:
      "f705bb9816cab47e0c725742419935a7d6aae51df3d54167a52d7b9b10d92fed",
    event_count: 2,
  },
  state: "unknown",
  reason: null,
  counts: {
    unknown: 1,
  },
  components: [
    {
      component_id: "api",
      repository_id: "27264cca2a5a46a885fc13e27ca17a2e",
      run_id: "9892ffe9fbc04fe8b612e2526e777d0b",
      attempt: 1,
      state: "unknown",
      recorded_status: "succeeded",
      reason:
        "The upstream run has no successful executable verification receipt",
      verification: "not_verified",
      admission_digest:
        "ef7232809047e7642df6ff6983d73ff540921093d527a4d3c5f0ce02403840aa",
      run_digest:
        "3ae0eef9b9d6f5e4598d44a2353c8bac2752b9a6021f6e2f1461f74ca1cb116f",
      source_base: "1111111111111111111111111111111111111111",
      branch: null,
      branch_receipt: null,
      checkpoint_revision: null,
      source_tree: null,
      source_tree_receipt: null,
      checkpoint_receipt: null,
      plan_digest: null,
      journal: {
        sequence: 0,
        digest:
          "0000000000000000000000000000000000000000000000000000000000000000",
        head: "0000000000000000000000000000000000000000000000000000000000000000",
      },
      tasks: [],
      checks: [],
      prior_attempts: [],
      depends_on: [],
    },
  ],
  milestones: "not_configured",
  waves: "not_configured",
  scm_freshness: "not_observed",
  limits: {
    attempts: 200,
    campaign_events: 1024,
    tasks_per_run: 128,
    journal_entries_per_run: 2048,
    run_events_per_run: 2048,
    snapshot_seconds: 10,
    evidence_json_bytes: 16777216,
  },
  digest: "fb1ee7f6bcf7b008b450dfe0536d14951e8b387fb70d08a2d5db6c782852fb28",
};
export const blockedReport = {
  version: 1,
  project_id: "ae8750c222d1466aad147b9bf351befb",
  campaign: {
    id: "2e8e2b4a26474740887dc7b45cf5eb46",
    recorded_status: "running",
    project_digest:
      "ecf9a5d041fc75dadb399b7dea051b7cce52930dad8561534ca92bec32136498",
    request_digest:
      "073886565296541cb708406669444ca52b85ff958d86bed00fb9693cb83318f3",
    journal_head:
      "ee837852bf7a61aeb7b3ce97c7c501b6382b4b673dcaa6ba24708dc4fcb3cd23",
    event_count: 2,
  },
  state: "blocked",
  reason: null,
  counts: {
    blocked: 1,
  },
  components: [
    {
      component_id: "api",
      repository_id: "4450356fabe041f19d9e76c52d40bd56",
      run_id: "23a07e4b92c4454ab0a40fb074af51af",
      attempt: 1,
      state: "blocked",
      recorded_status: "failed",
      reason:
        "The recorded run is blocked, stopped or awaiting an explicit operator decision.",
      verification: "not_verified",
      admission_digest:
        "b824c0bdf4ae7cfd06e21171faca0393f31ed26ed3bab4e64f4774d459657007",
      run_digest:
        "b11086c3d75f47054c60a9abda0ddb65434120a7f24abfaab4319d628cbf1581",
      source_base: "1111111111111111111111111111111111111111",
      branch: null,
      branch_receipt: null,
      checkpoint_revision: null,
      source_tree: null,
      source_tree_receipt: null,
      checkpoint_receipt: null,
      plan_digest: null,
      journal: {
        sequence: 0,
        digest:
          "0000000000000000000000000000000000000000000000000000000000000000",
        head: "0000000000000000000000000000000000000000000000000000000000000000",
      },
      tasks: [],
      checks: [],
      prior_attempts: [],
      depends_on: [],
    },
  ],
  milestones: "not_configured",
  waves: "not_configured",
  scm_freshness: "not_observed",
  limits: {
    attempts: 200,
    campaign_events: 1024,
    tasks_per_run: 128,
    journal_entries_per_run: 2048,
    run_events_per_run: 2048,
    snapshot_seconds: 10,
    evidence_json_bytes: 16777216,
  },
  digest: "9a6aac33ad8a08a2e15fe61bd2b798fa773e22caa6572bd6f7a4392c7b4410b6",
};
export const in_progressReport = {
  version: 1,
  project_id: "5375445dfad04ceeaa446df82df7cc32",
  campaign: {
    id: "a8d8b1f6bcd64e14addea7e1d7969748",
    recorded_status: "running",
    project_digest:
      "9f8364d4c443066fd10ecbe6a43efd0718cf077822256837d30c12ede2b2f45f",
    request_digest:
      "8387a4b0a3adbeb2c9d641a5443edce32fdee9df36072278f8e0b83448ca45be",
    journal_head:
      "f9da33747ee6a0b5ccdbbf6ad7d0eae3f46143abbd5be7d53c9e619b8fe0902e",
    event_count: 2,
  },
  state: "in_progress",
  reason: null,
  counts: {
    in_progress: 1,
  },
  components: [
    {
      component_id: "api",
      repository_id: "3b6b93c5da4e4a53a7d9f00ec3e994bc",
      run_id: "4bdc5f3429ef454d995b0b77a4df10ed",
      attempt: 1,
      state: "in_progress",
      recorded_status: "queued",
      reason: null,
      verification: "not_verified",
      admission_digest:
        "f454e26003c67b2f6b77b64bec4dc15e76e298439cea85f3a6d956cdc45bf7b3",
      run_digest:
        "473883d6c28a5fe9aed55d61e0b09909722665bc3e6883f4949ea19508dff786",
      source_base: "1111111111111111111111111111111111111111",
      branch: null,
      branch_receipt: null,
      checkpoint_revision: null,
      source_tree: null,
      source_tree_receipt: null,
      checkpoint_receipt: null,
      plan_digest: null,
      journal: {
        sequence: 0,
        digest:
          "0000000000000000000000000000000000000000000000000000000000000000",
        head: "0000000000000000000000000000000000000000000000000000000000000000",
      },
      tasks: [],
      checks: [],
      prior_attempts: [],
      depends_on: [],
    },
  ],
  milestones: "not_configured",
  waves: "not_configured",
  scm_freshness: "not_observed",
  limits: {
    attempts: 200,
    campaign_events: 1024,
    tasks_per_run: 128,
    journal_entries_per_run: 2048,
    run_events_per_run: 2048,
    snapshot_seconds: 10,
    evidence_json_bytes: 16777216,
  },
  digest: "b6ca59eeb029ea8427d21345b3bd94bf05e66b39e8df485497702161791752fb",
};

export const unavailableReport = {
  version: 1,
  project_id: "73f01946a9fa41a1bc5cce43a21fb653",
  campaign: {
    id: "80e2387757934834a0bd29ab3c439a5c",
    recorded_status: null,
    project_digest: null,
    request_digest: null,
    journal_head: null,
    event_count: null,
  },
  state: "unknown",
  reason: "evidence_integrity",
  counts: null,
  components: [],
  milestones: "not_configured",
  waves: "not_configured",
  scm_freshness: "not_observed",
  limits: {
    attempts: 200,
    campaign_events: 1024,
    tasks_per_run: 128,
    journal_entries_per_run: 2048,
    run_events_per_run: 2048,
    snapshot_seconds: 10,
    evidence_json_bytes: 16777216,
  },
  digest: "c70852560417eb74a19e4ff74226aea868165d8a83d4180a9fbec978d7a5dae6",
};
