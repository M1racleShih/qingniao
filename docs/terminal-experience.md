# The Qingniao terminal experience

Status: design draft. Qingniao has no runnable CLI yet. All commands below are
proposed syntax; names, models, timings, and usage are simulated. These examples
do not establish client or provider compatibility.

This guide is for contributors designing the first CLI. Use it to judge whether
a user can see the current route, understand a change, and recover from a failure
without additional explanation. Terminal presentation is part of the first
release's acceptance criteria.

## Visual direction

Use a compact Qingniao identity, a blue-green accent, deliberate spacing, and
aligned labels. Put the outcome first, followed by the destination and any action
the user needs to take. Keep identifiers easy to select and copy.

| Element | Treatment |
| --- | --- |
| Identity and selection | Blue-green accent; cyan in a basic ANSI palette |
| Main information | Terminal's default foreground and background |
| Success | `OK` or `RUNNING`, with green as an additional cue |
| Attention | `PENDING`, `PARTIAL`, or `UNKNOWN`, with yellow as a cue |
| Failure | `ERROR` or `INTERRUPTED`, with red as an additional cue |
| Supporting details | Spacing and indentation; text remains readable on light and dark backgrounds |

Color and symbols supplement text. Honor a non-empty `NO_COLOR` value and
`TERM=dumb`; retain the same information in plain text. Use ordinary monospace
fonts, with ASCII alternatives for decorative symbols.

The proposed interaction style uses ordinary commands and short keyboard-driven
prompts for choices. Preserve scrollback and copying. A full-screen terminal
workspace is not required for the first release.

## Status at a glance

Use a short summary and a compact route list. Show requested and actual upstream
model names separately. Provider configuration alone does not establish that a
connection is healthy; label checks with their result and time when available.

```text
$ qing status

qing  /  local gateway                         [DEMO: simulated data]

  RUNNING       2 providers configured · 2 routes configured

  REQUESTED MODEL    PROVIDER    UPSTREAM MODEL
  coding             demo-a      model-a
  review             demo-b      model-b

  LATEST REQUEST
  OK            coding → demo-a / model-a
  First token   420 ms              Total    2.4 s
  Input         1,240 tokens        Output   186 tokens
```

At 80 columns, align values without horizontal scrolling. At narrower widths,
stack labeled fields and wrap long identifiers; preserve complete destinations
and error messages. Wider terminals can use columns to reduce scanning effort.

## A route change with a clear result

Route updates take effect without restarting the gateway or an already connected
client. Requests admitted after the update is acknowledged use the updated
route; each request already admitted retains its original destination through
completion. The acknowledgement must come from the running gateway after the
update takes effect. Saving a file alone is not evidence that a live route changed.

Show the affected route, the old and new destinations, the affected client scope,
and what happens to requests in progress. If the gateway is stopped, distinguish
saved configuration from a live update. Global, client, and session scope are
still being designed; the scope label below is illustrative.

```text
$ qing route set coding --provider demo-b --model model-b

qing  /  route update                          [DEMO: simulated data]

  OK            Route updated · live

  Requested     coding
  Previous      demo-a / model-a
  Current       demo-b / model-b
  Scope         Clients using this route

  New requests  Use demo-b / model-b
  In progress   1 request continues on demo-a / model-a
```

A client task can contain several requests. A model may initiate a tool call
before a route update, with the tool-result request reaching a different model
afterward. Verify that round trip for the documented client and provider pair;
request-boundary updates alone do not establish compatibility.

## Loading, empty, and failure states

During an interactive connection check, show the operation and provider being
checked. Use a small activity indicator while work is pending, then replace it
with a final result. Display percentages only when progress can be measured.
Cancelling a prompt should leave a readable result and restore the terminal.

An empty route list should say `No routes configured` and identify the setup
action. An empty request history should say `No requests recorded` and explain
that requests must pass through Qingniao to appear.

Errors should identify the failing operation and a useful next step. Retain
known usage and mark missing values explicitly, even when the response fails.

```text
$ qing requests

qing  /  latest request                        [DEMO: simulated data]

  INTERRUPTED   Upstream connection closed before completion

  Requested     coding
  Destination   demo-a / model-a
  Input         1,240 tokens
  Output        Unknown · final usage was not received

  Next          Check the connection to demo-a.
```

Errors and request summaries must exclude credentials and full prompts or
responses. Preserve the existing default of recording request metadata only.

## Pipes and redirected output

When output is redirected or the terminal is basic, emit readable plain text.
Disable color, animated indicators, and cursor control in that output stream.
Keep successful command results on stdout and diagnostics on stderr; failures
return a non-zero exit status. Scripts must not need to strip decorative output
to find a command's result.

## Verification before release

These checks are planned; the mockups above do not pass them on their own.

| Check | Evidence required |
| --- | --- |
| Visual consistency | Review real CLI captures on light and dark terminals at 80 and 120 columns |
| Narrow output | At 40 columns, long names and errors wrap without losing essential information |
| Plain output | Exercise `NO_COLOR`, `TERM=dumb`, and redirected streams; no unwanted escape sequences or cursor movement |
| Complete states | Walk through loading, success, empty, failure, unknown usage, and prompt cancellation |
| Route feedback | Hold request A open, update its route, and issue B after acknowledgement; A keeps the old destination and B uses the new one |
| Concurrent updates | Each request uses one complete configuration, including its endpoint, model, and credential reference |
| Real task | A new user completes setup, a request, a route change, inspection, and configuration restoration from the documented terminal flow |

The first gateway and CLI will use Python. The command hierarchy, exact palette,
and terminal library remain design choices. Select libraries after reviewing the
CLI flow; this draft does not require a particular framework.
