-- SQLite authority schema version 1.
PRAGMA foreign_keys = ON;

CREATE TABLE project_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    application TEXT NOT NULL CHECK (application = 'charlie-pinboard'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    revision INTEGER NOT NULL CHECK (revision >= 0),
    host_epoch INTEGER NOT NULL CHECK (host_epoch >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE artifact_refs (
    artifact_ref_id INTEGER PRIMARY KEY,
    artifact_key TEXT NOT NULL,
    artifact_revision INTEGER NOT NULL CHECK (artifact_revision >= 1),
    kind TEXT NOT NULL CHECK (kind IN (
        'requirements', 'plan', 'design', 'brief', 'result', 'blocker', 'evidence'
    )),
    relative_path TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    accepted_revision INTEGER NOT NULL CHECK (accepted_revision >= 0),
    created_at TEXT NOT NULL,
    UNIQUE (kind, artifact_key, artifact_revision),
    UNIQUE (artifact_ref_id, kind)
) STRICT;

CREATE TABLE work_items (
    item_id TEXT PRIMARY KEY,
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('native', 'legacy-import')),
    user_label TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'intake', 'ready', 'active', 'paused', 'blocked', 'deferred', 'review',
        'done', 'superseded', 'dropped'
    )),
    timing TEXT CHECK (timing IN ('must-now', 'cheaper-now', 'safe-to-defer')),
    source TEXT,
    trigger TEXT,
    why_it_matters TEXT,
    effect TEXT,
    unlock TEXT,
    outcome_evidence TEXT,
    next_action TEXT,
    notes TEXT,
    scope_revision INTEGER NOT NULL CHECK (scope_revision >= 1),
    scope_digest TEXT NOT NULL CHECK (length(scope_digest) = 64),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    origin_created_at TEXT,
    origin_updated_at TEXT,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (item_id, state, outcome_evidence),
    CHECK ((state IN ('done', 'superseded', 'dropped')) = (outcome_evidence IS NOT NULL)),
    FOREIGN KEY (item_id, scope_revision, scope_digest)
        REFERENCES item_scope_revisions(item_id, scope_revision, scope_digest)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (origin_kind = 'legacy-import' OR (
        source IS NOT NULL AND trigger IS NOT NULL AND why_it_matters IS NOT NULL
        AND effect IS NOT NULL AND unlock IS NOT NULL AND notes IS NOT NULL
        AND origin_created_at IS NOT NULL AND origin_updated_at IS NOT NULL
    ))
) STRICT;

CREATE TABLE item_scope_revisions (
    item_id TEXT NOT NULL REFERENCES work_items(item_id) ON DELETE CASCADE,
    scope_revision INTEGER NOT NULL CHECK (scope_revision >= 1),
    scope_digest TEXT NOT NULL CHECK (length(scope_digest) = 64),
    accepted_project_revision INTEGER NOT NULL CHECK (accepted_project_revision >= 0),
    accepted_at TEXT NOT NULL,
    PRIMARY KEY (item_id, scope_revision),
    UNIQUE (item_id, scope_revision, scope_digest)
) STRICT;

CREATE TABLE item_dependencies (
    item_id TEXT NOT NULL REFERENCES work_items(item_id) ON DELETE CASCADE,
    dependency_id TEXT NOT NULL REFERENCES work_items(item_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (item_id, dependency_id),
    UNIQUE (item_id, position),
    CHECK (item_id <> dependency_id)
) STRICT;

CREATE TABLE item_artifacts (
    item_id TEXT NOT NULL REFERENCES work_items(item_id) ON DELETE CASCADE,
    artifact_ref_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('requirements', 'plan', 'design', 'evidence')),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (item_id, artifact_ref_id),
    UNIQUE (item_id, role, position),
    FOREIGN KEY (artifact_ref_id, role)
        REFERENCES artifact_refs(artifact_ref_id, kind)
) STRICT;

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES work_items(item_id),
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('native', 'legacy-import')),
    state TEXT NOT NULL CHECK (state IN ('active', 'paused', 'blocked', 'review', 'done', 'closed')),
    branch TEXT NOT NULL,
    base_revision TEXT NOT NULL,
    provenance TEXT NOT NULL,
    brief_artifact_ref_id INTEGER NOT NULL,
    brief_artifact_kind TEXT NOT NULL DEFAULT 'brief' CHECK (brief_artifact_kind = 'brief'),
    result_artifact_ref_id INTEGER,
    result_artifact_kind TEXT CHECK (result_artifact_kind = 'result'),
    blocker_artifact_ref_id INTEGER,
    blocker_artifact_kind TEXT CHECK (blocker_artifact_kind = 'blocker'),
    candidate_revision TEXT,
    candidate_recorded_at TEXT,
    accepted_scope_revision INTEGER NOT NULL CHECK (accepted_scope_revision >= 1),
    accepted_scope_digest TEXT NOT NULL CHECK (length(accepted_scope_digest) = 64),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    origin_created_at TEXT,
    origin_updated_at TEXT,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (attempt_id, item_id),
    FOREIGN KEY (brief_artifact_ref_id, brief_artifact_kind)
        REFERENCES artifact_refs(artifact_ref_id, kind),
    FOREIGN KEY (result_artifact_ref_id, result_artifact_kind)
        REFERENCES artifact_refs(artifact_ref_id, kind),
    FOREIGN KEY (blocker_artifact_ref_id, blocker_artifact_kind)
        REFERENCES artifact_refs(artifact_ref_id, kind),
    FOREIGN KEY (item_id, accepted_scope_revision, accepted_scope_digest)
        REFERENCES item_scope_revisions(item_id, scope_revision, scope_digest),
    CHECK ((result_artifact_ref_id IS NULL) = (result_artifact_kind IS NULL)),
    CHECK ((blocker_artifact_ref_id IS NULL) = (blocker_artifact_kind IS NULL)),
    CHECK ((candidate_revision IS NULL) = (candidate_recorded_at IS NULL)),
    CHECK (origin_kind = 'legacy-import' OR (
        (state = 'review' AND candidate_revision IS NOT NULL)
        OR state = 'done'
        OR (state IN ('active', 'paused', 'blocked') AND candidate_revision IS NULL)
        OR state = 'closed'
    )),
    CHECK (origin_kind = 'legacy-import' OR (
        origin_created_at IS NOT NULL AND origin_updated_at IS NOT NULL
    ))
) STRICT;

CREATE UNIQUE INDEX one_live_attempt_per_item
ON attempts(item_id)
WHERE state NOT IN ('done', 'closed');

CREATE TABLE planning_impacts (
    impact_id TEXT PRIMARY KEY,
    source_item_id TEXT NOT NULL REFERENCES work_items(item_id),
    source_attempt_id TEXT,
    source_scope_revision INTEGER NOT NULL CHECK (source_scope_revision >= 1),
    source_scope_digest TEXT NOT NULL CHECK (length(source_scope_digest) = 64),
    primary_target_item_id TEXT NOT NULL,
    primary_target_position INTEGER NOT NULL CHECK (primary_target_position = 0),
    summary TEXT NOT NULL CHECK (summary <> ''),
    evidence TEXT NOT NULL CHECK (evidence <> ''),
    recorded_project_revision INTEGER NOT NULL CHECK (recorded_project_revision >= 1),
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (source_attempt_id, source_item_id)
        REFERENCES attempts(attempt_id, item_id),
    FOREIGN KEY (source_item_id, source_scope_revision, source_scope_digest)
        REFERENCES item_scope_revisions(item_id, scope_revision, scope_digest),
    FOREIGN KEY (impact_id, primary_target_item_id, primary_target_position)
        REFERENCES planning_impact_obligations(impact_id, target_item_id, target_position)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE planning_impact_obligations (
    impact_id TEXT NOT NULL REFERENCES planning_impacts(impact_id) ON DELETE CASCADE,
    target_item_id TEXT NOT NULL REFERENCES work_items(item_id),
    target_position INTEGER NOT NULL CHECK (target_position >= 0),
    observed_scope_revision INTEGER NOT NULL CHECK (observed_scope_revision >= 1),
    observed_scope_digest TEXT NOT NULL CHECK (length(observed_scope_digest) = 64),
    status TEXT NOT NULL CHECK (status IN ('unresolved', 'resolved')),
    disposition TEXT CHECK (disposition IN (
        'unchanged', 'revised', 'blocked', 'deferred', 'dropped', 'superseded'
    )),
    evaluated_scope_revision INTEGER CHECK (evaluated_scope_revision >= 1),
    evaluated_scope_digest TEXT CHECK (
        evaluated_scope_digest IS NULL OR length(evaluated_scope_digest) = 64
    ),
    resulting_scope_revision INTEGER CHECK (resulting_scope_revision >= 2),
    resulting_scope_digest TEXT CHECK (
        resulting_scope_digest IS NULL OR length(resulting_scope_digest) = 64
    ),
    primary_replacement_item_id TEXT,
    primary_replacement_position INTEGER CHECK (
        primary_replacement_position IS NULL OR primary_replacement_position = 0
    ),
    outcome_evidence TEXT CHECK (outcome_evidence IS NULL OR outcome_evidence <> ''),
    reason TEXT,
    resolved_project_revision INTEGER CHECK (resolved_project_revision >= 1),
    recorded_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (impact_id, target_item_id),
    UNIQUE (impact_id, target_position),
    UNIQUE (impact_id, target_item_id, target_position),
    UNIQUE (impact_id, target_item_id, disposition),
    FOREIGN KEY (target_item_id, observed_scope_revision, observed_scope_digest)
        REFERENCES item_scope_revisions(item_id, scope_revision, scope_digest),
    FOREIGN KEY (target_item_id, evaluated_scope_revision, evaluated_scope_digest)
        REFERENCES item_scope_revisions(item_id, scope_revision, scope_digest),
    FOREIGN KEY (target_item_id, resulting_scope_revision, resulting_scope_digest)
        REFERENCES item_scope_revisions(item_id, scope_revision, scope_digest),
    FOREIGN KEY (
        impact_id, target_item_id, primary_replacement_item_id, primary_replacement_position
    ) REFERENCES planning_impact_replacements(
        impact_id, target_item_id, replacement_item_id, position
    ) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (target_item_id, disposition, outcome_evidence)
        REFERENCES work_items(item_id, state, outcome_evidence)
        DEFERRABLE INITIALLY DEFERRED,
    CHECK (
        (status = 'unresolved' AND disposition IS NULL
            AND evaluated_scope_revision IS NULL AND evaluated_scope_digest IS NULL
            AND resulting_scope_revision IS NULL AND resulting_scope_digest IS NULL
            AND primary_replacement_item_id IS NULL AND primary_replacement_position IS NULL
            AND outcome_evidence IS NULL
            AND reason IS NULL AND resolved_project_revision IS NULL AND resolved_at IS NULL)
        OR
        (status = 'resolved' AND disposition IS NOT NULL
            AND evaluated_scope_revision IS NOT NULL AND evaluated_scope_digest IS NOT NULL
            AND reason IS NOT NULL AND reason <> ''
            AND resolved_project_revision IS NOT NULL AND resolved_at IS NOT NULL
            AND (
                (disposition = 'revised'
                    AND resulting_scope_revision > evaluated_scope_revision
                    AND resulting_scope_digest IS NOT NULL
                    AND primary_replacement_item_id IS NULL
                    AND primary_replacement_position IS NULL
                    AND outcome_evidence IS NULL)
                OR
                (disposition = 'superseded'
                    AND resulting_scope_revision IS NULL AND resulting_scope_digest IS NULL
                    AND primary_replacement_item_id IS NOT NULL
                    AND primary_replacement_position = 0
                    AND outcome_evidence IS NOT NULL)
                OR
                (disposition = 'dropped'
                    AND resulting_scope_revision IS NULL AND resulting_scope_digest IS NULL
                    AND primary_replacement_item_id IS NULL
                    AND primary_replacement_position IS NULL
                    AND outcome_evidence IS NOT NULL)
                OR
                (disposition NOT IN ('revised', 'dropped', 'superseded')
                    AND resulting_scope_revision IS NULL AND resulting_scope_digest IS NULL
                    AND primary_replacement_item_id IS NULL
                    AND primary_replacement_position IS NULL
                    AND outcome_evidence IS NULL)
            ))
    )
) STRICT;

CREATE TABLE planning_impact_replacements (
    impact_id TEXT NOT NULL,
    target_item_id TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'superseded' CHECK (disposition = 'superseded'),
    replacement_item_id TEXT NOT NULL REFERENCES work_items(item_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (impact_id, target_item_id, replacement_item_id),
    UNIQUE (impact_id, target_item_id, position),
    UNIQUE (impact_id, target_item_id, replacement_item_id, position),
    FOREIGN KEY (impact_id, target_item_id, disposition)
        REFERENCES planning_impact_obligations(impact_id, target_item_id, disposition),
    CHECK (replacement_item_id <> target_item_id)
) STRICT;

CREATE TABLE proposals (
    proposal_id TEXT PRIMARY KEY,
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('native', 'legacy-import')),
    created_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    source_task_id TEXT NOT NULL,
    user_label TEXT NOT NULL,
    trigger TEXT NOT NULL,
    why_it_matters TEXT NOT NULL,
    relation_kind TEXT NOT NULL CHECK (relation_kind IN (
        'independent', 'prerequisite', 'follow-up', 'duplicate', 'contradiction'
    )),
    relation_item_id TEXT REFERENCES work_items(item_id),
    effect TEXT NOT NULL,
    unlock TEXT NOT NULL,
    urgency_evidence TEXT NOT NULL,
    disposition TEXT CHECK (disposition IN ('accepted', 'merged', 'returned', 'rejected')),
    disposition_target_item_id TEXT REFERENCES work_items(item_id),
    disposition_reason TEXT,
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    origin_disposed_at TEXT,
    disposition_recorded_at TEXT,
    CHECK ((disposition IS NULL) = (disposition_recorded_at IS NULL)),
    CHECK (origin_kind = 'legacy-import' OR (
        (disposition IS NULL AND origin_disposed_at IS NULL)
        OR (disposition IS NOT NULL AND origin_disposed_at IS NOT NULL)
    ))
) STRICT;

CREATE TABLE proposal_evidence (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    selector TEXT NOT NULL,
    PRIMARY KEY (proposal_id, position)
) STRICT;

CREATE TABLE proposal_freshness (
    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    assumption TEXT NOT NULL,
    PRIMARY KEY (proposal_id, position)
) STRICT;

CREATE TABLE resources (
    resource_id TEXT PRIMARY KEY,
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('native', 'legacy-import')),
    kind TEXT NOT NULL CHECK (
        kind <> '' AND kind = lower(kind)
        AND kind NOT GLOB '*[^a-z0-9-]*'
        AND substr(kind, 1, 1) <> '-'
        AND substr(kind, -1, 1) <> '-'
        AND instr(kind, '--') = 0
    ),
    scope TEXT NOT NULL CHECK (scope = 'portable-definition'),
    allocation_mode TEXT NOT NULL CHECK (allocation_mode = 'exclusive-instance'),
    description TEXT NOT NULL,
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    origin_created_at TEXT,
    origin_updated_at TEXT,
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (origin_kind = 'legacy-import' OR (
        origin_created_at IS NOT NULL AND origin_updated_at IS NOT NULL
    ))
) STRICT;

CREATE TABLE item_resources (
    item_id TEXT NOT NULL REFERENCES work_items(item_id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (item_id, resource_id),
    UNIQUE (item_id, position)
) STRICT;

CREATE TABLE resource_instances (
    instance_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
    host_id TEXT NOT NULL,
    discovery_kind TEXT NOT NULL,
    discovery_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    recorded_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (instance_id, resource_id, host_id),
    UNIQUE (instance_id, host_id),
    UNIQUE (host_id, discovery_kind, discovery_fingerprint),
    CHECK (discovery_kind <> ''),
    CHECK (discovery_fingerprint <> '')
) STRICT;

CREATE TABLE resource_instance_locators (
    instance_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL,
    locator_schema TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    observation_generation INTEGER NOT NULL CHECK (observation_generation >= 1),
    observation_digest TEXT NOT NULL CHECK (length(observation_digest) = 64),
    observed_at TEXT NOT NULL,
    FOREIGN KEY (instance_id, host_id)
        REFERENCES resource_instances(instance_id, host_id) ON DELETE CASCADE,
    CHECK (locator_schema <> ''),
    CHECK (locator_json <> ''),
    UNIQUE (instance_id, host_id, observation_generation, observation_digest)
) STRICT;

CREATE TABLE resource_reservation_counters (
    instance_id TEXT PRIMARY KEY REFERENCES resource_instances(instance_id) ON DELETE CASCADE,
    generation_high_water INTEGER NOT NULL CHECK (generation_high_water >= 0)
) STRICT;

CREATE TABLE resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    attempt_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'revoked-pending-recovery', 'revoked')),
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    created_at TEXT NOT NULL,
    ended_at TEXT,
    UNIQUE (instance_id, generation),
    UNIQUE (reservation_id, instance_id, attempt_id, host_id, generation),
    FOREIGN KEY (instance_id, resource_id, host_id)
        REFERENCES resource_instances(instance_id, resource_id, host_id),
    FOREIGN KEY (instance_id)
        REFERENCES resource_reservation_counters(instance_id),
    FOREIGN KEY (attempt_id, item_id)
        REFERENCES attempts(attempt_id, item_id),
    FOREIGN KEY (item_id, resource_id)
        REFERENCES item_resources(item_id, resource_id),
    CHECK ((status IN ('active', 'revoked-pending-recovery')) = (ended_at IS NULL))
) STRICT;

CREATE UNIQUE INDEX one_live_reservation_per_instance
ON resource_reservations(instance_id)
WHERE status IN ('active', 'revoked-pending-recovery');

CREATE UNIQUE INDEX one_live_reservation_per_requirement
ON resource_reservations(attempt_id, resource_id)
WHERE status IN ('active', 'revoked-pending-recovery');

CREATE TABLE coordination_lease (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    lease_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'revoked'))
) STRICT;

CREATE TABLE attempt_lease_counters (
    attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id) ON DELETE CASCADE,
    generation_high_water INTEGER NOT NULL CHECK (generation_high_water >= 0)
) STRICT;

CREATE TABLE attempt_lease_generations (
    attempt_id TEXT NOT NULL REFERENCES attempt_lease_counters(attempt_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    lease_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    PRIMARY KEY (attempt_id, generation),
    UNIQUE (attempt_id, lease_id, generation, task_id, host_id)
) STRICT;

CREATE TABLE attempt_leases (
    attempt_id TEXT PRIMARY KEY REFERENCES attempt_lease_counters(attempt_id) ON DELETE CASCADE,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'revoked', 'expired')),
    FOREIGN KEY (attempt_id, generation)
        REFERENCES attempt_lease_generations(attempt_id, generation)
) STRICT;

CREATE TABLE resource_use_leases (
    reservation_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    reservation_generation INTEGER NOT NULL CHECK (reservation_generation >= 1),
    attempt_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    instance_subject_revision INTEGER NOT NULL CHECK (instance_subject_revision >= 0),
    observation_generation INTEGER NOT NULL CHECK (observation_generation >= 1),
    observation_digest TEXT NOT NULL CHECK (length(observation_digest) = 64),
    task_id TEXT NOT NULL,
    attempt_lease_id TEXT NOT NULL,
    attempt_lease_generation INTEGER NOT NULL CHECK (attempt_lease_generation >= 1),
    lease_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    generation_kind TEXT NOT NULL CHECK (generation_kind IN ('grant', 'fence')),
    host_epoch INTEGER NOT NULL CHECK (host_epoch >= 1),
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'released', 'revoked', 'expired')),
    PRIMARY KEY (reservation_id, generation),
    UNIQUE (
        reservation_id, generation, lease_id, task_id, attempt_id, host_id,
        attempt_lease_id, attempt_lease_generation, generation_kind
    ),
    FOREIGN KEY (reservation_id, instance_id, attempt_id, host_id, reservation_generation)
        REFERENCES resource_reservations(reservation_id, instance_id, attempt_id, host_id, generation),
    FOREIGN KEY (attempt_id, attempt_lease_id, attempt_lease_generation, task_id, host_id)
        REFERENCES attempt_lease_generations(attempt_id, lease_id, generation, task_id, host_id),
    CHECK (generation_kind = 'grant' OR status = 'revoked')
) STRICT;

CREATE UNIQUE INDEX one_live_use_lease_per_reservation
ON resource_use_leases(reservation_id)
WHERE status = 'active';

CREATE TABLE resource_mutation_intents (
    intent_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL,
    reservation_generation INTEGER NOT NULL CHECK (reservation_generation >= 1),
    instance_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    host_id TEXT NOT NULL,
    resource_use_generation INTEGER NOT NULL CHECK (resource_use_generation >= 1),
    resource_use_lease_id TEXT NOT NULL,
    resource_use_generation_kind TEXT NOT NULL CHECK (resource_use_generation_kind = 'grant'),
    task_id TEXT NOT NULL,
    attempt_lease_id TEXT NOT NULL,
    attempt_lease_generation INTEGER NOT NULL CHECK (attempt_lease_generation >= 1),
    start_instance_subject_revision INTEGER NOT NULL CHECK (start_instance_subject_revision >= 0),
    start_observation_generation INTEGER NOT NULL CHECK (start_observation_generation >= 1),
    start_observation_digest TEXT NOT NULL CHECK (length(start_observation_digest) = 64),
    policy_schema TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    policy_digest TEXT NOT NULL CHECK (length(policy_digest) = 64),
    status TEXT NOT NULL CHECK (status IN ('planned', 'accepted', 'reconciled', 'human-preserved', 'abandoned')),
    recorded_at TEXT NOT NULL,
    resolved_at TEXT,
    result_observation_generation INTEGER CHECK (result_observation_generation >= 2),
    result_observation_digest TEXT CHECK (result_observation_digest IS NULL OR length(result_observation_digest) = 64),
    evidence_schema TEXT,
    evidence_json TEXT,
    evidence_digest TEXT CHECK (evidence_digest IS NULL OR length(evidence_digest) = 64),
    disposition_task_id TEXT,
    disposition_reason TEXT,
    FOREIGN KEY (reservation_id, instance_id, attempt_id, host_id, reservation_generation)
        REFERENCES resource_reservations(reservation_id, instance_id, attempt_id, host_id, generation),
    FOREIGN KEY (
        reservation_id, resource_use_generation, resource_use_lease_id, task_id, attempt_id, host_id,
        attempt_lease_id, attempt_lease_generation, resource_use_generation_kind
    ) REFERENCES resource_use_leases(
        reservation_id, generation, lease_id, task_id, attempt_id, host_id,
        attempt_lease_id, attempt_lease_generation, generation_kind
    ),
    FOREIGN KEY (attempt_id, attempt_lease_id, attempt_lease_generation, task_id, host_id)
        REFERENCES attempt_lease_generations(attempt_id, lease_id, generation, task_id, host_id),
    CHECK (policy_schema <> '' AND policy_json <> ''),
    CHECK (
        (evidence_schema IS NULL AND evidence_json IS NULL AND evidence_digest IS NULL)
        OR
        (evidence_schema IS NOT NULL AND evidence_json IS NOT NULL AND evidence_digest IS NOT NULL)
    ),
    CHECK (
        (disposition_task_id IS NULL AND disposition_reason IS NULL)
        OR
        (disposition_task_id IS NOT NULL AND disposition_reason IS NOT NULL AND disposition_reason <> '')
    ),
    CHECK (
        (status = 'planned' AND resolved_at IS NULL
            AND result_observation_generation IS NULL AND result_observation_digest IS NULL
            AND evidence_schema IS NULL AND evidence_json IS NULL AND evidence_digest IS NULL
            AND disposition_task_id IS NULL AND disposition_reason IS NULL)
        OR
        (status IN ('accepted', 'reconciled') AND resolved_at IS NOT NULL
            AND result_observation_generation > start_observation_generation
            AND result_observation_digest IS NOT NULL
            AND evidence_schema IS NOT NULL AND evidence_json IS NOT NULL AND evidence_digest IS NOT NULL
            AND disposition_task_id IS NULL AND disposition_reason IS NULL)
        OR
        (status = 'human-preserved' AND resolved_at IS NOT NULL
            AND result_observation_generation > start_observation_generation
            AND result_observation_digest IS NOT NULL
            AND disposition_task_id IS NOT NULL AND disposition_reason IS NOT NULL
            AND disposition_reason <> '')
        OR
        (status = 'abandoned' AND resolved_at IS NOT NULL
            AND result_observation_generation IS NULL AND result_observation_digest IS NULL
            AND evidence_schema IS NULL AND evidence_json IS NULL AND evidence_digest IS NULL
            AND disposition_task_id IS NOT NULL AND disposition_reason IS NOT NULL
            AND disposition_reason <> '')
    )
) STRICT;

CREATE UNIQUE INDEX one_planned_mutation_intent_per_use_lease
ON resource_mutation_intents(reservation_id, resource_use_generation)
WHERE status = 'planned';

CREATE TABLE current_focus (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    item_id TEXT REFERENCES work_items(item_id),
    attempt_id TEXT REFERENCES attempts(attempt_id),
    next_action TEXT NOT NULL,
    subject_revision INTEGER NOT NULL CHECK (subject_revision >= 0),
    CHECK (attempt_id IS NULL OR item_id IS NOT NULL),
    FOREIGN KEY (attempt_id, item_id) REFERENCES attempts(attempt_id, item_id)
) STRICT;

CREATE TABLE transition_history (
    history_id INTEGER PRIMARY KEY,
    project_revision INTEGER NOT NULL UNIQUE CHECK (project_revision >= 1),
    action_id TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    artifact_ref_id INTEGER,
    artifact_kind TEXT CHECK (artifact_kind = 'evidence'),
    authorization_kind TEXT NOT NULL,
    actor_task_id TEXT,
    actor_host_id TEXT,
    input_schema TEXT NOT NULL,
    input_json TEXT NOT NULL,
    outcome_schema TEXT NOT NULL,
    outcome_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    FOREIGN KEY (artifact_ref_id, artifact_kind)
        REFERENCES artifact_refs(artifact_ref_id, kind),
    CHECK ((artifact_ref_id IS NULL) = (artifact_kind IS NULL)),
    CHECK (action_kind NOT IN ('legacy-import', 'legacy-cleanup') OR artifact_ref_id IS NOT NULL)
) STRICT;

CREATE UNIQUE INDEX one_legacy_cleanup_history_per_cutover
ON transition_history(action_id)
WHERE action_kind = 'legacy-cleanup';
