# FAQ

## Is this a replacement for a human WCAG audit?

No. It is a scanner and evidence collector. It finds many automated
failures quickly and gives auditors a structured list of `cantTell`
items to review, but WCAG conformance still requires human judgment.

## Which engine should I use?

Use axe-core for fast iterative work. Use `--engine all` for baselines
and audit sweeps where coverage matters more than runtime.

## Why are there both violations and incompletes?

`failed` means an automated engine found a definite failure. `cantTell`
means the engine could not decide automatically. Incompletes are common
for contrast checks involving gradients, images, pseudo-elements, and
overlaps.

## Why does a finding id look like `sc-1.4.3`?

Deduped reports group cross-engine findings by normalized primary tag.
`sc-1.4.3` means WCAG Success Criterion 1.4.3, Contrast Minimum.

## Why does JSONL matter?

Long scans can produce huge reports. JSONL lets the scanner write one
complete page result at a time, flush it immediately, and rebuild HTML
or JSON views later without keeping everything in memory.

## Can it scan logged-in pages?

Yes. Write a login plugin that implements `async def login(context,
config) -> bool`, then configure it under `auth.login_script`.

## Does it follow links from URL-list mode?

No. `--urls FILE` scans exactly those URLs. This is useful for curated
template samples and regression checks.

## Why did MCP reject `http://127.0.0.1`?

MCP may be driven by an LLM client, so it rejects private/local targets
by default to avoid SSRF. Use `A11Y_CATSCAN_MCP_ALLOW_PRIVATE=1` only in
trusted local test setups.

## Can I suppress findings?

Yes, with an allowlist YAML. Use it for known-acceptable items and write
a reason. Avoid suppressing multi-engine confirmed failures unless you
have manually verified the page.

## Where should generated reports live?

Use `output_dir`, usually `./reports`. Reports are gitignored by
default because they can contain page snippets and authenticated URLs.
