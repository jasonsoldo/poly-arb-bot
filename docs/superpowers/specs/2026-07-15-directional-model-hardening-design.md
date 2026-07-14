# Directional Model Hardening Design

## Goal

Prevent repeated correlated Directional/Lottery losses without inventing new probability coefficients from three effective close-window samples.

## Design

The C++ reference engine will store at most one model sample per wall-clock second. Updates inside the same second replace the current bucket, so volatility and sample count no longer depend on exchange tick rate. It will publish both effective sample count and covered seconds.

Python will require a configurable minimum covered duration before producing a probability. Insufficient coverage remains visible as an evaluation with `decision=REJECT` and `reason=model_sample_span_insufficient`. Historical one-minute fallback models publish their known time span and remain eligible.

The portfolio default will allow one non-paired position per `close_ts` across Directional and Lottery. Paired-lock remains independent because it carries no directional outcome exposure.

## Non-Goals

- Do not change the Gaussian probability formula.
- Do not tune momentum, imbalance, EV, fee, or buffer values from this small sample.
- Do not enable real orders.

## Verification

- Regression test proves repeated ticks in one second are bucketed.
- Regression test proves insufficient time coverage fails closed.
- Regression test proves the default close-window limit blocks a second correlated strategy position.
- Python suite, C++ build, JavaScript parse, Bash syntax, and Git checks pass.
