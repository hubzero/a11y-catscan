/**
 * alfa-engine.mjs — Siteimprove Alfa engine for axe-spyder.
 *
 * Long-running subprocess that connects to an existing Chromium browser
 * via CDP and runs Alfa rules on pages already loaded by the Python crawler.
 *
 * Protocol:
 *   Init:   {"cdp": "ws://...", "level": "aa"}
 *   Scan:   {"pageId": "TARGET_ID"}
 *   Quit:   {"quit": true}
 *
 *   Ready:  {"ok": true, "ready": true, "rules": N, "level": "aa"}
 *   Result: {"ok": true, "pageId": "...", "violations": [...], "incomplete": [...], "passes": N}
 */

import { Playwright } from "@siteimprove/alfa-playwright";
import { Audit } from "@siteimprove/alfa-act";
import { Rules } from "@siteimprove/alfa-rules";
import { chromium } from "playwright";
import readline from "readline";

// WCAG success criteria → level mapping
const WCAG_LEVELS = {
  "1.1.1": "a",
  "1.2.1": "a", "1.2.2": "a", "1.2.3": "a",
  "1.2.4": "aa", "1.2.5": "aa",
  "1.2.6": "aaa", "1.2.7": "aaa", "1.2.8": "aaa", "1.2.9": "aaa",
  "1.3.1": "a", "1.3.2": "a", "1.3.3": "a",
  "1.3.4": "aa", "1.3.5": "aa",
  "1.3.6": "aaa",
  "1.4.1": "a", "1.4.2": "a",
  "1.4.3": "aa", "1.4.4": "aa", "1.4.5": "aa",
  "1.4.6": "aaa", "1.4.7": "aaa", "1.4.8": "aaa", "1.4.9": "aaa",
  "1.4.10": "aa", "1.4.11": "aa", "1.4.12": "aa", "1.4.13": "aa",
  "2.1.1": "a", "2.1.2": "a",
  "2.1.3": "aaa", "2.1.4": "a",
  "2.2.1": "a", "2.2.2": "a",
  "2.2.3": "aaa", "2.2.4": "aaa", "2.2.5": "aaa", "2.2.6": "aaa",
  "2.3.1": "a",
  "2.3.2": "aaa", "2.3.3": "aaa",
  "2.4.1": "a", "2.4.2": "a", "2.4.3": "a", "2.4.4": "a",
  "2.4.5": "aa", "2.4.6": "aa", "2.4.7": "aa",
  "2.4.8": "aaa", "2.4.9": "aaa", "2.4.10": "aaa",
  "2.4.11": "aa", "2.4.12": "aaa", "2.4.13": "aaa",
  "2.5.1": "a", "2.5.2": "a", "2.5.3": "a", "2.5.4": "a",
  "2.5.5": "aaa", "2.5.6": "aaa",
  "2.5.7": "aa", "2.5.8": "aa",
  "3.1.1": "a",
  "3.1.2": "aa",
  "3.1.3": "aaa", "3.1.4": "aaa", "3.1.5": "aaa", "3.1.6": "aaa",
  "3.2.1": "a", "3.2.2": "a",
  "3.2.3": "aa", "3.2.4": "aa",
  "3.2.5": "aaa", "3.2.6": "a",
  "3.3.1": "a", "3.3.2": "a",
  "3.3.3": "aa", "3.3.4": "aa",
  "3.3.5": "aaa", "3.3.6": "aaa",
  "3.3.7": "a", "3.3.8": "aa", "3.3.9": "a",
  "4.1.1": "a", "4.1.2": "a", "4.1.3": "aa",
};

const LEVEL_INCLUDES = {
  a: new Set(["a"]),
  aa: new Set(["a", "aa"]),
  aaa: new Set(["a", "aa", "aaa"]),
};

function filterRulesByLevel(rules, level) {
  const allowed = LEVEL_INCLUDES[level] || LEVEL_INCLUDES["aa"];
  return rules.filter((rule) => {
    const reqs = rule.toJSON().requirements || [];
    const wcag = reqs.filter((r) => r.type === "criterion");
    if (wcag.length === 0) return true;
    return wcag.some((r) => {
      const scLevel = WCAG_LEVELS[r.chapter];
      return scLevel && allowed.has(scLevel);
    });
  });
}

function normalizeOutcome(j) {
  const rule_uri = j.rule?.uri || "";
  const rule_id = rule_uri.split("/").pop() || rule_uri;
  const wcag = (j.rule?.requirements || [])
    .filter((r) => r.type === "criterion")
    .map((r) => r.chapter);

  let target = "";
  if (j.target?.name) {
    target = j.target.name;
    if (j.target.attributes) {
      for (const attr of j.target.attributes) {
        if (attr.name === "class" && attr.value) {
          target += "." + attr.value.split(" ").join(".");
          break;
        }
      }
    }
  }

  let message = "";
  if (j.expectations) {
    try {
      for (const [_, exp] of j.expectations) {
        if (exp.type === "err" && exp.error?.message) {
          message = exp.error.message;
          break;
        }
      }
    } catch {
      message = JSON.stringify(j.expectations).substring(0, 200);
    }
  }

  return { rule: rule_id, uri: rule_uri, wcag, target, html: target, message };
}

let browser = null;
let filteredRules = null;

async function init(cdpUrl, level) {
  const allRules = [...Rules].map(([_, rule]) => rule);
  filteredRules = filterRulesByLevel(allRules, level || "aa");
  browser = await chromium.connectOverCDP(cdpUrl);
}

async function scanPage(pageId) {
  // Find the page by its CDP target ID across all contexts
  for (const ctx of browser.contexts()) {
    for (const page of ctx.pages()) {
      // Match by target ID from CDP
      const targetId =
        page._impl_obj?._initializer?.guid ||
        page.url(); // fallback to URL match
      // The Python side sends us the page URL since target IDs
      // are hard to extract consistently across bindings
      if (page.url() === pageId || targetId === pageId) {
        const alfaPage = await Playwright.toPage(page);
        const outcomes = await Audit.of(alfaPage, filteredRules).evaluate();

        const violations = [];
        const incomplete = [];
        let passes = 0;

        for (const outcome of outcomes) {
          const j = outcome.toJSON();
          if (j.outcome === "failed") {
            violations.push(normalizeOutcome(j));
          } else if (j.outcome === "cantTell") {
            incomplete.push(normalizeOutcome(j));
          } else if (j.outcome === "passed") {
            passes++;
          }
        }

        return { ok: true, pageId, violations, incomplete, passes };
      }
    }
  }
  return { ok: false, pageId, error: "Page not found in browser" };
}

// Main loop
const rl = readline.createInterface({ input: process.stdin });
let initialized = false;

for await (const line of rl) {
  try {
    const req = JSON.parse(line.trim());

    if (!initialized) {
      if (!req.cdp) {
        process.stdout.write(
          JSON.stringify({ ok: false, error: "First message must include 'cdp' WebSocket URL" }) + "\n"
        );
        continue;
      }
      await init(req.cdp, req.level || "aa");
      initialized = true;
      process.stdout.write(
        JSON.stringify({
          ok: true,
          ready: true,
          rules: filteredRules.length,
          level: req.level || "aa",
        }) + "\n"
      );
      continue;
    }

    if (req.pageId) {
      const result = await scanPage(req.pageId);
      process.stdout.write(JSON.stringify(result) + "\n");
    } else if (req.quit) {
      break;
    }
  } catch (e) {
    process.stdout.write(
      JSON.stringify({ ok: false, error: e.message }) + "\n"
    );
  }
}

// Disconnect (don't close — Python owns the browser)
if (browser) {
  try { browser.close(); } catch {}
}
