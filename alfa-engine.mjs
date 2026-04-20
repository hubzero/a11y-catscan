/**
 * alfa-engine.mjs — Siteimprove Alfa engine for axe-spyder.
 *
 * Long-running subprocess that receives URLs on stdin (one JSON per line)
 * and returns normalized accessibility results on stdout.
 *
 * Protocol:
 *   Init:   {"cookies": [...], "level": "aa"}
 *   Scan:   {"url": "https://..."}
 *   Quit:   {"quit": true}
 *
 *   Ready:  {"ok": true, "ready": true, "rules": N}
 *   Result: {"ok": true, "url": "...", "violations": [...], "incomplete": [...], "passes": N}
 *
 * Each violation/incomplete has:
 *   {rule, uri, wcag, target, message, impact}
 */

import { Playwright } from "@siteimprove/alfa-playwright";
import { Audit } from "@siteimprove/alfa-act";
import { Rules } from "@siteimprove/alfa-rules";
import { chromium } from "playwright";
import readline from "readline";

// WCAG success criteria → level mapping.
// Source: https://www.w3.org/TR/WCAG21/
// Format: "chapter" → "a", "aa", or "aaa"
const WCAG_LEVELS = {
  // Perceivable
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
  // Operable
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
  // Understandable
  "3.1.1": "a",
  "3.1.2": "aa",
  "3.1.3": "aaa", "3.1.4": "aaa", "3.1.5": "aaa", "3.1.6": "aaa",
  "3.2.1": "a", "3.2.2": "a",
  "3.2.3": "aa",
  "3.2.4": "aa",
  "3.2.5": "aaa", "3.2.6": "a",
  "3.3.1": "a", "3.3.2": "a",
  "3.3.3": "aa", "3.3.4": "aa",
  "3.3.5": "aaa", "3.3.6": "aaa",
  "3.3.7": "a", "3.3.8": "aa", "3.3.9": "a",
  // Robust
  "4.1.1": "a", "4.1.2": "a", "4.1.3": "aa",
};

// Level hierarchy: aa includes a, aaa includes aa and a
const LEVEL_INCLUDES = {
  a: new Set(["a"]),
  aa: new Set(["a", "aa"]),
  aaa: new Set(["a", "aa", "aaa"]),
};

function filterRulesByLevel(rules, level) {
  const allowed = LEVEL_INCLUDES[level] || LEVEL_INCLUDES["aa"];

  return rules.filter((rule) => {
    const reqs = rule.toJSON().requirements || [];
    const wcagCriteria = reqs.filter((r) => r.type === "criterion");

    // Rules with no WCAG criteria (best practices) — include them
    if (wcagCriteria.length === 0) return true;

    // Include if ANY of the rule's criteria are at or below the target level
    return wcagCriteria.some((r) => {
      const scLevel = WCAG_LEVELS[r.chapter];
      return scLevel && allowed.has(scLevel);
    });
  });
}

let browser = null;
let context = null;
let filteredRules = null;

async function init(cookies, level) {
  // Filter rules by WCAG level
  const allRules = [...Rules].map(([_, rule]) => rule);
  filteredRules = filterRulesByLevel(allRules, level || "aa");

  browser = await chromium.launch({ headless: true });
  const contextOpts = { viewport: { width: 1280, height: 1024 } };
  context = await browser.newContext(contextOpts);
  if (cookies && cookies.length > 0) {
    await context.addCookies(cookies);
  }
}

function normalizeOutcome(j) {
  const rule_uri = j.rule?.uri || "";
  const rule_id = rule_uri.split("/").pop() || rule_uri;

  const wcag = (j.rule?.requirements || [])
    .filter((r) => r.type === "criterion")
    .map((r) => r.chapter);

  let target = "";
  let html = "";
  if (j.target) {
    if (j.target.name) {
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
    html = target;
  }

  let message = "";
  if (j.expectations) {
    // Extract the error message from expectations
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

  return {
    rule: rule_id,
    uri: rule_uri,
    wcag,
    target,
    html,
    message,
  };
}

async function scan(url) {
  const page = await context.newPage();
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });

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

    return { ok: true, url, violations, incomplete, passes };
  } catch (e) {
    return { ok: false, url, error: e.message };
  } finally {
    await page.close();
  }
}

// Main loop: read JSON lines from stdin
const rl = readline.createInterface({ input: process.stdin });

let initialized = false;

for await (const line of rl) {
  try {
    const req = JSON.parse(line.trim());

    if (!initialized) {
      await init(req.cookies || [], req.level || "aa");
      initialized = true;
      process.stdout.write(
        JSON.stringify({
          ok: true,
          ready: true,
          rules: filteredRules.length,
          level: req.level || "aa",
        }) + "\n"
      );
    }

    if (req.url) {
      const result = await scan(req.url);
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

if (browser) await browser.close();
