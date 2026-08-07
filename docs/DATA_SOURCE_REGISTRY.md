# BridgeSAT Data Source Registry

## 1. Status

- Registry version: `0.1.0`
- Verified on: `2026-08-06`
- Machine-readable registry: `config/sources.yaml`
- Default rule: **deny ingestion unless both the source and the requested action are explicitly approved**

This document is the human-readable authority for BridgeSAT data acquisition. It separates product content, candidate generation, evaluation data, and reference-only material. A source approved for one purpose is not automatically approved for another.

Rights statements and terms can change. Recheck a source before every public release and whenever upstream terms change.

---

## 2. Usage classes

### 2.1 Product content

Content displayed to students, indexed in the production RAG system, or included in an offline pack.

### 2.2 Candidate generation

Source code or data used to generate internal drafts. Drafts must be independently rewritten, answer-checked, and reviewed before product use.

### 2.3 Evaluation

Held-out material used only to evaluate retrieval, reasoning, reading comprehension, or memory behavior. Evaluation items must remain outside production retrieval and training prompts.

### 2.4 Reference only

Material that may be read or linked but must not be copied, cached, embedded, transformed, or indexed.

---

## 3. Status vocabulary

| Status | Meaning |
|---|---|
| `approved` | The listed actions are approved; item-level educational review still applies. |
| `approved_with_item_review` | The collection is eligible, but each item needs rights and suitability review. |
| `candidate_generation_only` | May create internal drafts; raw source material is not student-facing content. |
| `evaluation_only` | Isolated benchmark use only. |
| `manual_license_review_required` | Disabled until a reviewer approves the exact item and intended use. |
| `reference_only` | Links, taxonomy reference, or human reading only. |
| `prohibited` | The action is blocked. |

---

## 4. Required provenance

Every imported item must retain:

- `source_id`;
- upstream item ID;
- canonical URL;
- title and creator;
- exact rights statement or license;
- allowed use;
- acquisition timestamp;
- content hash;
- review status and reviewer ID;
- required attribution;
- content-pack version.

No item may enter the student-facing RAG index or offline pack until rights, educational, answer, age-suitability, and accessibility reviews pass.

---

## 5. Approved and conditional sources

## 5.1 BridgeSAT original content

**ID:** `bridgesat_original`  
**Status:** `approved`

Primary uses:

- original SAT-style questions;
- micro-lessons and worked examples;
- three-level hints;
- misconception descriptions;
- synthetic learners and golden evaluation events.

Conditions:

- record authorship and AI assistance;
- do not reproduce or closely paraphrase restricted SAT questions;
- independently verify every answer;
- require educational review before publication;
- keep evaluation splits separate from authoring and prompt examples.

---

## 5.2 DeepMind Mathematics Dataset

**ID:** `deepmind_mathematics_dataset`  
**Official source:** `https://github.com/google-deepmind/mathematics_dataset`  
**License:** Apache License 2.0  
**Status:** `candidate_generation_only`

The project generates school-level mathematical question-and-answer pairs across algebra, arithmetic, comparison, measurement, numbers, polynomials, probability, and other modules.

Allowed:

- clone or install the official generator;
- generate an internal candidate pool;
- use generated structures for internal tests;
- transform selected structures into independently written BridgeSAT questions.

Required pipeline:

```text
official generator
  -> SAT-scope filtering
  -> deterministic answer verification
  -> original SAT-style rewrite
  -> misconception-based distractors
  -> three hints and worked solution
  -> educational review
  -> approval
```

Raw generated output must not be published automatically or described as official SAT content.

---

## 5.3 Project Gutenberg

**ID:** `project_gutenberg`  
**Machine-access policy:** `https://dev.gutenberg.org/policy/robot_access.html`  
**Permissions guidance:** `https://www.gutenberg.org/policy/permission`  
**Status:** `approved_with_item_review`

The majority of Project Gutenberg ebooks are public domain in the United States. Each work and each deployment jurisdiction still require review.

Allowed acquisition methods:

- official machine-readable catalog;
- Robot Harvest;
- official mirrors or OPDS;
- manual download of a selected work.

Blocked acquisition method:

- automated crawling of ordinary human-facing pages.

Item requirements:

- preserve ebook ID, title, author, rights statement, and canonical URL;
- verify rights in the intended deployment jurisdiction;
- select short, age-appropriate excerpts;
- create original questions and explanations;
- avoid implying Project Gutenberg endorsement;
- retain attribution and source evidence.

Preferred use: reviewed public-domain reading passages.

---

## 5.4 Library of Congress Free to Use and Reuse

**ID:** `library_of_congress_free_to_use`  
**Collection:** `https://www.loc.gov/free-to-use/`  
**Data package:** `https://data.labs.loc.gov/free-to-use/`  
**Status:** `approved_with_item_review`

The Library describes these curated sets as content believed to be public domain, to have no known restrictions, or to have been cleared for public use. Collection- and item-level rights statements still control.

Allowed acquisition methods:

- official API;
- official downloadable package;
- item-level manual import;
- official IIIF or delivery endpoints when permitted.

Item requirements:

- preserve the Library item ID, canonical URL, rights advisory, and credit line;
- inspect third-party content embedded in an item;
- review historical terminology and age suitability;
- provide alt text for images, maps, tables, and charts;
- create original questions rather than treating metadata as validated learning content.

Preferred use: reviewed reading passages, historical sources, maps, images, and data displays.

---

## 6. Evaluation and internal-reference sources

## 6.1 GSM8K

**ID:** `gsm8k`  
**Official source:** `https://github.com/openai/grade-school-math`  
**License:** MIT License  
**Repository state:** archived on `2026-04-08`  
**Status:** `evaluation_only`

Allowed:

- isolated mathematical-reasoning evaluation;
- study of multi-step solution structure;
- internal development of hint and misconception taxonomies;
- benchmark reporting with attribution.

BridgeSAT restrictions:

- no raw GSM8K questions in the production question bank;
- no evaluation items in authoring prompts or model-training data;
- no claim that GSM8K performance equals SAT performance;
- pin the exact upstream revision because the repository is archived.

---

## 6.2 Belebele

**ID:** `belebele`  
**Official source:** `https://github.com/facebookresearch/belebele`  
**Benchmark license:** CC BY-SA 4.0  
**Status:** `evaluation_only`

Allowed:

- passage-retrieval evaluation;
- English reading-comprehension evaluation;
- separately reported multilingual accessibility experiments;
- benchmark reporting with required attribution.

Restrictions:

- do not use benchmark items as production practice questions;
- do not use them as question-generation training data;
- exclude the separately assembled training set, which has different license terms;
- store the benchmark in an isolated evaluation namespace.

---

## 6.3 QuALITY

**ID:** `quality`  
**Official source:** `https://github.com/nyu-mll/quality`  
**Status:** `manual_license_review_required`

QuALITY includes an article-level `license` field, but that does not automatically approve every article, annotation, redistribution, evaluation, or RAG use.

Before any item is enabled:

- verify the article license value;
- verify rights to the questions and annotations;
- verify redistribution and AI/RAG ingestion rights;
- store a license snapshot;
- separately approve product and evaluation use.

The importer must remain disabled by default.

---

## 7. Reference-only and restricted sources

## 7.1 College Board SAT resources

**ID:** `college_board_sat`  
**Agreement:** `https://satsuite.collegeboard.org/k12-educators/educator-experience/student-data-privacy-agreement`  
**Status:** `reference_only`

College Board states that SAT Suite Question Bank questions and explanations are proprietary and provides a limited classroom/internal-reporting license. Its agreement prohibits uploading, caching, reproducing, modifying, displaying, editing, altering, or enhancing Question Bank content without express permission.

Allowed:

- public content-domain names as taxonomy references;
- external links to official resources;
- independently authored questions that do not copy protected content.

Blocked:

- crawling or downloading the Question Bank;
- copying or modifying official questions and explanations;
- caching, embedding, RAG ingestion, or offline redistribution;
- generating a derivative question from a copied official item.

---

## 7.2 Khan Academy

**ID:** `khan_academy`  
**Policy:** `https://support.khanacademy.org/hc/en-us/articles/42929097425037-What-s-allowed-and-not-allowed-on-Khan-Academy`  
**Status:** `reference_only`

Khan Academy's current policy prohibits bots or scraping tools used to copy or extract site data and prohibits using its content or data to train, test, or develop AI or machine-learning systems.

Allowed:

- external links;
- human personal learning;
- high-level pedagogy inspiration without copying content.

Blocked:

- crawling, copying, embedding, RAG ingestion, AI training, AI evaluation, and offline redistribution.

---

## 7.3 OpenStax

**ID:** `openstax`  
**Status:** `reference_only`

Many OpenStax books use Creative Commons licenses, but current book pages also state that content may not be used to train large language models or otherwise be ingested into LLM or generative-AI offerings without OpenStax permission.

BridgeSAT therefore disables automated RAG/LLM ingestion unless written permission is obtained for the specific work and use.

Allowed without additional permission:

- external links;
- human reference while independently writing explanations.

Blocked without written permission:

- crawling, embedding, generative-AI ingestion, offline packing, and product redistribution.

---

## 8. Decision matrix

| Source | Product RAG | Offline pack | Candidate generation | Evaluation | Automated access |
|---|---:|---:|---:|---:|---:|
| BridgeSAT original | Yes | Yes | Yes | Isolated split | Local authoring |
| DeepMind Mathematics | After rewrite/review | After rewrite/review | Yes | Yes | Git/package |
| Project Gutenberg | Item approval | Item approval | Yes | Yes | Catalog/Harvest only |
| Library of Congress | Item approval | Item approval | Yes | Yes | Official API/package |
| GSM8K | No | No | Internal reference | Yes | Git/dataset import |
| Belebele | No | No | No | Yes | Git/dataset import |
| QuALITY | Disabled pending review | Disabled | Disabled | Item approval | Importer disabled |
| College Board | No | No | No copied content | No copied content | Disabled |
| Khan Academy | No | No | No | No | Disabled |
| OpenStax | No without permission | No without permission | Human reference | No ingestion | Disabled |

---

## 9. Review workflow

```text
source proposed
  -> source rights review
  -> sources.yaml entry
  -> approved acquisition method
  -> item import
  -> immutable provenance record
  -> item rights review
  -> educational and age-suitability review
  -> answer, hint, and accessibility validation
  -> approved index
  -> versioned offline pack
```

Review states:

```text
discovered
source_review_pending
source_approved
item_review_pending
educational_review_pending
approved
rejected
withdrawn
```

### 9.1 Withdrawal

When rights or correctness concerns arise:

1. mark the item `withdrawn`;
2. remove it from active retrieval and future packs;
3. publish a content-pack revocation record;
4. stop new sessions selecting it;
5. remove indexed and cached derivatives;
6. preserve only permitted audit metadata;
7. document whether historical attempts remain valid.

---

## 10. Initial acquisition batches

### Batch A — mathematics

- generate 500–1,000 candidate structures with DeepMind Mathematics Dataset;
- filter to the frozen BridgeSAT skill scope;
- deduplicate by normalized expression and answer;
- rewrite 100–150 candidates;
- approve 60–80 original questions.

### Batch B — reading

- select 10–15 Project Gutenberg works through official interfaces;
- select 10–15 Library of Congress items with clear rights records;
- extract short, age-appropriate passages;
- create 30–40 original questions;
- retain exact item provenance.

### Batch C — evaluation

- isolate a GSM8K subset for mathematical reasoning;
- isolate an English Belebele subset for retrieval/comprehension;
- keep all benchmark IDs out of product content and training prompts;
- create BridgeSAT-owned golden policy, RAG, memory, and sync scenarios.

---

## 11. Change control

Changing a source status requires:

- a review record;
- updated `last_verified` and `review_expires` dates;
- evidence URLs or stored rights snapshots;
- reviewer identity;
- changed action list;
- migration or withdrawal plan for indexed content.

Unknown statuses, missing rights evidence, expired reviews, or ambiguous intended uses must disable ingestion.

