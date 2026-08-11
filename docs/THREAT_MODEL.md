# BridgeSAT Threat Model

## 1. Scope

This threat model covers the mobile PWA, FastAPI application, PostgreSQL database, optional model providers, content ingestion pipeline, RAG index, Mnemis memory backend, and offline synchronization.

The system may be used by minors, so learner privacy and dignity are primary security assets.

---

## 2. Protected assets

```text
learner identity and pseudonymous identifiers
learning events and answer history
long-term learner memory
mastery and diagnostic estimates
content and answer keys
session tokens and API keys
source and license records
Agent policy integrity
offline event queue
Mnemis memory graph
deployment and database backups
```

---

## 3. Trust boundaries

```text
untrusted browser input
untrusted crawled or imported content
trusted application policy
trusted reviewed content pack
authoritative PostgreSQL store
derived RAG and Mnemis indexes
external LLM and embedding providers
public network
```

Retrieved documents and student text are data, never trusted instructions.

---

## 4. Threat actors

- unauthenticated internet users;
- one learner attempting to access another learner's data;
- malicious or compromised content source;
- malicious prompt text embedded in documents;
- abusive client sending forged events;
- compromised dependency or model provider;
- accidental operator error;
- stolen device with cached learner state.

---

## 5. Threats and mitigations

### 5.1 Cross-learner data leakage

Threats include changing a student ID in an API request, broad memory retrieval without learner filtering, and cached responses shared across students.

Mitigations:

- derive student scope from the authenticated session, not request path alone;
- enforce student scope at repository level;
- include student scope in all cache and index keys;
- query Mnemis with mandatory student filters;
- test insecure direct-object-reference attempts;
- never expose internal sequential database IDs.

### 5.2 RAG prompt injection

External content may contain instructions such as “ignore the system prompt” or request data exfiltration.

Mitigations:

- treat retrieved content as quoted evidence only;
- separate instructions from retrieved data in prompts;
- strip scripts, hidden text, and executable markup;
- reject suspicious instruction-like content for manual review;
- tool execution is controlled by application policy, not document text;
- no retrieved text can change permissions, endpoints, or memory state;
- citation validation maps generated claims to approved content.

### 5.3 Memory poisoning

Threat: malicious or accidental input creates false persistent learner facts.

Mitigations:

- raw text cannot directly create stable memory;
- require supporting event IDs and confidence thresholds;
- distinguish observations from inferences;
- preserve contradictions;
- allow learner correction and forgetting;
- Mnemis output cannot directly mutate authoritative facts;
- free-text LLM labels remain low confidence until independently confirmed.

### 5.4 Forged offline events

Threat: a client submits fabricated results or mastery changes.

Mitigations:

- clients submit observations, never authoritative mastery;
- server recomputes scores and projections;
- validate question and content versions;
- use scoped device registration and session tokens;
- cap event rates and batch sizes;
- maintain append-only audit events;
- flag impossible sequences for review.

### 5.5 XSS

Mitigations:

- escape all student and content text by default;
- use a strict Content Security Policy;
- prohibit inline script in reviewed content;
- sanitize any allowed rich text;
- do not use `innerHTML` with untrusted content;
- set `HttpOnly`, `Secure`, and appropriate `SameSite` cookies when cookies are used.

### 5.6 CSRF

Mitigations:

- prefer bearer tokens for the PWA API or same-site cookies with CSRF tokens;
- reject state-changing requests without the required token;
- restrict allowed origins;
- do not permit wildcard credentialed CORS.

### 5.7 Crawler SSRF

Mitigations:

- only crawl approved registered sources;
- block private, loopback, link-local, multicast, and reserved addresses;
- revalidate every redirect destination;
- enforce scheme allowlist;
- use response-size and timeout limits;
- obey robots rules;
- fail closed when the source or license is not approved.

### 5.8 Unauthorized answer-key access

Mitigations:

- offline packs contain only content necessary for the experience;
- answer keys are not exposed before submission in the UI;
- recognize that fully offline scoring cannot provide perfect secrecy;
- do not market offline content as secure high-stakes testing;
- use it only for formative practice.

### 5.9 Secret exposure

Mitigations:

- no keys in repository or client bundle;
- environment variables or deployment secret store;
- redact secrets from logs;
- rotate compromised keys;
- optional model integrations disabled when keys are absent.

### 5.10 Dependency and supply-chain risk

Mitigations:

- lock dependency versions;
- generate a software bill of materials where practical;
- run dependency vulnerability scans;
- minimize optional frameworks in the default path;
- verify package sources;
- document Mnemis, graph-store, model, and embedding dependencies separately.

### 5.11 Denial of service and cost exhaustion

Mitigations:

- per-IP and per-student rate limits;
- strict Mnemis and LLM timeouts;
- maximum retrieval and token budgets;
- bounded crawler queues;
- request body limits;
- circuit breakers for optional services;
- local deterministic fallback.

### 5.12 Data recovery exposure

Mitigations:

- backups exclude unnecessary secrets;
- restrict backup file permissions;
- encrypt backups when stored outside the host;
- test restore procedures;
- apply deletion policies to backups according to the documented retention window.

---

## 6. Privacy rules for learner data

- use pseudonymous IDs;
- names are optional;
- do not collect school, address, phone, precise location, or unnecessary demographics;
- separate identity records from learning events;
- do not send raw learner text to external models unless explicitly required and disclosed;
- send the minimum fields necessary;
- provide data export, correction, and deletion;
- do not sell or use learner records for advertising;
- do not present inferred memories as immutable traits.

Competition demos use fictional learner profiles.

---

## 7. External model contract

Allowed tasks:

```text
rewrite an approved explanation
translate approved content
classify free-text reasoning as a low-confidence suggestion
summarize already validated episodes
assist complex retrieval planning
```

Prohibited tasks:

```text
change the answer key
write mastery directly
create a stable learner fact without evidence thresholds
authorize content or licenses
execute arbitrary tools
change session state outside the bounded policy
send learner data to another destination
```

Every model call declares task type, model version, prompt version, input field allowlist, maximum tokens, timeout, fallback, and output schema.

Model output must pass schema and safety validation before display or use.

---

## 8. Logging policy

Logs may contain request IDs, pseudonymous student IDs when needed, event types, latency, error categories, and backend or fallback status.

Logs must not contain access tokens, API keys, full free-text learner responses by default, complete memory summaries unless redacted, or answer keys in routine request logs.

---

## 9. Security headers and web defaults

Recommended production headers:

```text
Content-Security-Policy
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Permissions-Policy
Strict-Transport-Security
frame-ancestors 'none'
```

CORS allows only configured origins. HTTPS is mandatory outside localhost.

---

## 10. Security acceptance tests

Required tests:

1. student A cannot read or delete student B data;
2. injected document instructions do not alter Agent behavior;
3. free text cannot create a stable memory alone;
4. repeated forged sync events do not duplicate mastery changes;
5. crawler blocks localhost and private-network redirects;
6. HTML content cannot execute script in the PWA;
7. secrets are absent from repository and built client assets;
8. LLM timeout triggers deterministic fallback;
9. deletion removes learner data from PostgreSQL and Mnemis retrieval;
10. oversized requests and excessive retrieval loops are rejected.

---

## 11. Incident response

```text
detect
  -> contain optional integration
  -> preserve audit evidence
  -> revoke affected tokens or keys
  -> notify the operator
  -> correct or delete poisoned memory
  -> reindex if needed
  -> document root cause
  -> add regression test
```

For the competition deployment, the operator must be able to disable external models, Mnemis, embeddings, and crawler ingestion independently without disabling the local learning loop.
