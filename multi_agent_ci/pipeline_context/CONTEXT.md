# Pipeline Audit Log
Generated: 2026-04-28T03:42:08.502267+00:00
Company: Chuwi
Total Retries: 0

# Pipeline Audit Report

## Pipeline Summary
- **Company**: Chuwi
- **Pipeline Start Time**: April 28, 2026, 03:39:05 UTC
- **Total Duration**: 2 minutes 37 seconds

## Agent Timeline

| Timestamp                          | Event                     | Agent            | Details                                      |
|------------------------------------|---------------------------|-------------------|----------------------------------------------|
| 2026-04-28T03:39:05.040498+00:00  | pipeline_start            | ui_intake         |                                              |
| 2026-04-28T03:39:05.041525+00:00  | routing_decision          | supervisor         |                                              |
| 2026-04-28T03:39:05.041525+00:00  | routing                   | supervisor         | Next agent: planner                          |
| 2026-04-28T03:39:05.042852+00:00  | start                     | planner            |                                              |
| 2026-04-28T03:39:10.733812+00:00  | complete                  | planner            | Model: gpt-4o-mini, Latency: 5688 ms       |
| 2026-04-28T03:39:10.734811+00:00  | routing_decision          | supervisor         |                                              |
| 2026-04-28T03:39:10.734811+00:00  | routing                   | supervisor         | Next agent: researcher                       |
| 2026-04-28T03:39:10.735811+00:00  | start                     | researcher         | Starting parallel web + financial research   |
| 2026-04-28T03:39:10.736819+00:00  | start                     | web_search         |                                              |
| 2026-04-28T03:39:10.737825+00:00  | start                     | financial          |                                              |
| 2026-04-28T03:39:12.821088+00:00  | complete                  | financial          | Status: ok, Latency: 2083 ms                |
| 2026-04-28T03:39:15.841512+00:00  | complete                  | web_search         | Queries made: 5, Results count: 20         |
| 2026-04-28T03:39:15.846576+00:00  | synthesis_start           | researcher         |                                              |
| 2026-04-28T03:39:15.846576+00:00  | start                     | synthesis          |                                              |
| 2026-04-28T03:39:30.669863+00:00  | complete                  | synthesis          | Model: gpt-4o-mini, Latency: 14822 ms      |
| 2026-04-28T03:39:30.669863+00:00  | human_gate                | researcher         |                                              |
| 2026-04-28T03:39:30.669863+00:00  | waiting_for_human         | human_validator    |                                              |
| 2026-04-28T03:41:30.684410+00:00  | auto_accepted_timeout     | human_validator    |                                              |
| 2026-04-28T03:41:30.684410+00:00  | complete                  | researcher         | Research chars: 2980                         |
| 2026-04-28T03:41:30.696981+00:00  | routing_decision          | supervisor         |                                              |
| 2026-04-28T03:41:30.696981+00:00  | routing                   | supervisor         | Next agent: writer                           |
| 2026-04-28T03:41:30.699973+00:00  | start                     | writer             |                                              |
| 2026-04-28T03:41:42.092899+00:00  | complete                  | writer             | Model: gpt-4o-mini, Latency: 11391 ms      |

## Performance Metrics

| Agent        | Event      | Latency (ms) | Completion Time (ms) | Data Volume (chars) | Additional Info                      |
|--------------|------------|---------------|-----------------------|----------------------|--------------------------------------|
| planner      | complete   | 5688          | 5689                  | 1245                 | Model: gpt-4o-mini                   |
| financial    | complete   | 2083          | N/A                   | 882                  | Status: ok, RTR Estimate: 0.85      |
| web_search   | complete   | 5104          | N/A                   | 3974                 | Queries made: 5, Results count: 20  |
| synthesis     | complete   | 14822         | 14823                 | N/A                  | Model: gpt-4o-mini, Source traces: 11 |
| researcher    | complete   | N/A           | N/A                   | 2980                 |                                      |
| writer       | complete   | 11391         | 11392                 | 2905                 | Model: gpt-4o-mini                   |

## Errors & Retries
- **Total Retries**: 0
- **Human Validator Events**:
  - Waiting for human validation at 03:39:30 UTC
  - Auto-accepted timeout at 03:41:30 UTC

This report summarizes the pipeline execution for Chuwi, detailing the timeline of events, performance metrics, and any errors or retries encountered during the process.