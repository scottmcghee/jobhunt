---
id: build_infra_devx
title: Build infrastructure & developer productivity
use_when: [build systems, ci/cd, developer experience, developer productivity, platform engineering, release engineering]
---
At Acme Learning, our CI pipeline had grown to forty minutes, and engineers had stopped trusting it: they batched changes, skipped tests locally, and merged on Fridays out of habit. I made build speed a platform-team goal with a public dashboard, moved builds to remote caching and ephemeral runners, and split the monolithic test suite by ownership so teams could fix their own flaky tests. Pipeline time fell to nine minutes, deploys went from weekly to several a day, and the number of reverted releases dropped by half. The lesson I took from it is that developer productivity is a product, and it needs a roadmap and users like any other.
