/**
 * alfa-engine.mjs — Siteimprove Alfa engine for axe-spyder.
 *
 * Long-running subprocess that receives URLs on stdin (one JSON per line)
 * and returns normalized accessibility results on stdout.
 *
 * Protocol:
 *   Input:  {"url": "https://...", "cookies": [...]}
 *   Output: {"ok": true, "violations": [...], "incomplete": [...], "passes": N}
 *
 * Each violation/incomplete has:
 *   {rule, uri, wcag, target, message, impact}
 */

import { Playwright } from "@siteimprove/alfa-playwright";
import { Audit } from "@siteimprove/alfa-act";
import { Rules } from "@siteimprove/alfa-rules";
import { chromium } from "playwright";
import readline from "readline";

// Extract rule objects from the versioned Map
const ruleList = [...Rules].map(([_, rule]) => rule);

let browser = null;
let context = null;

async function init(cookies) {
  browser = await chromium.launch({ headless: true });
  const contextOpts = { viewport: { width: 1280, height: 1024 } };
  if (cookies && cookies.length > 0) {
    // Create context with cookies for auth
    context = await browser.newContext(contextOpts);
    await context.addCookies(cookies);
  } else {
    context = await browser.newContext(contextOpts);
  }
}

function normalizeOutcome(j) {
  const rule_uri = j.rule?.uri || "";
  // Extract rule ID from URI: .../sia-r69 → sia-r69
  const rule_id = rule_uri.split("/").pop() || rule_uri;

  // Extract WCAG criteria
  const wcag = (j.rule?.requirements || [])
    .filter((r) => r.type === "criterion")
    .map((r) => r.chapter);

  // Extract target element info
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
    // Reconstruct a simple HTML snippet
    html = target;
  }

  // Extract message from expectations
  let message = "";
  if (j.expectations) {
    message = JSON.stringify(j.expectations).substring(0, 200);
  }

  return {
    rule: rule_id,
    uri: rule_uri,
    wcag: wcag,
    target: target,
    html: html,
    message: message,
  };
}

async function scan(url) {
  const page = await context.newPage();
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });

    const alfaPage = await Playwright.toPage(page);
    const outcomes = await Audit.of(alfaPage, ruleList).evaluate();

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
      await init(req.cookies || []);
      initialized = true;
      // Signal ready
      process.stdout.write(
        JSON.stringify({ ok: true, ready: true, rules: ruleList.length }) +
          "\n"
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
