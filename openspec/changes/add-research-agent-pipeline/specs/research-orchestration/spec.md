# Spec Delta

## Purpose

Defines how the assistant turns a natural-language research question into a cited report by
coordinating specialised agents (planning, research, analysis, critique and writing).

## ADDED Requirements

### Requirement: Question decomposition
The system SHALL decompose every research question into between 1 and the configured maximum
number of focused sub-questions before any evidence is gathered.

#### Scenario: Plan is bounded
- **WHEN** a question is submitted with `max_subquestions` set to 3
- **THEN** the plan contains at least 1 and at most 3 sub-questions

#### Scenario: Planner output is unusable
- **WHEN** the planning step returns no usable sub-questions
- **THEN** the system falls back to researching the original question as a single sub-question

### Requirement: Parallel evidence gathering
The system SHALL research sub-questions concurrently, never exceeding the configured
concurrency limit, and SHALL record for each sub-question a summary and the sources it used.

#### Scenario: Concurrency limit respected
- **WHEN** 5 sub-questions are planned and the concurrency limit is 2
- **THEN** no more than 2 sub-questions are being researched at any moment

#### Scenario: One sub-question fails
- **WHEN** research for one sub-question raises an error and others succeed
- **THEN** the run continues, the failed sub-question is recorded with its error, and the report notes the gap

#### Scenario: All sub-questions fail
- **WHEN** research fails for every sub-question
- **THEN** the run fails with an error stating that no evidence could be gathered

### Requirement: Critique and revision loop
The system SHALL have a critic review each draft analysis and SHALL revise the analysis at
most the configured number of revision rounds, stopping early when the critic approves.

#### Scenario: Critic approves immediately
- **WHEN** the critic approves the first draft
- **THEN** no revision round is executed

#### Scenario: Revision budget exhausted
- **WHEN** the critic never approves and `max_revisions` is 2
- **THEN** exactly 2 revision rounds run and the report is produced from the last revision

### Requirement: Cited report
The system SHALL produce a Markdown report containing a title, an executive summary, the
findings, and a numbered, deduplicated list of sources that the findings cite.

#### Scenario: Duplicate sources merged
- **WHEN** two sub-questions cite the same URL
- **THEN** the URL appears exactly once in the report's source list

### Requirement: Progress events
The system SHALL emit an ordered progress event when each stage starts and finishes
(planning, each sub-question, analysis, each critique, writing), each carrying the stage
name, a message and a timestamp.

#### Scenario: Events observed for a full run
- **WHEN** a run completes successfully
- **THEN** the emitted events begin with the planning stage and end with a completed event

### Requirement: Offline operation
The system SHALL support a deterministic offline mode that requires no network access or
credentials and produces a complete report.

#### Scenario: Mock provider
- **WHEN** the provider is configured as `mock` and a question is submitted
- **THEN** a complete report is produced without any outbound network call

### Requirement: Model refusals are surfaced
The system SHALL treat a model refusal as a stage failure carrying the refusal category,
rather than treating the empty response as valid content.

#### Scenario: Researcher refused
- **WHEN** the model refuses a research sub-question
- **THEN** that sub-question is recorded as failed with a message that names the refusal
