# Getting Started

## Quickstart: `firefly/skeleton`

The fastest way to a booting, cached LaraFly app is the installer and `firefly/skeleton` template bundled in
`fireflyframework/larafly` — a Laravel 13
application pre-wired with the Firefly family, a `#[Controller]` welcome page, a sample
`#[RestController]`/`#[Service]` pair, and `firefly:cache` already wired into `post-create-project-cmd`:

```bash
composer global require fireflyframework/larafly
firefly new my-app
cd my-app
php artisan firefly:cache
php artisan firefly:serve
```

The installer invokes Composer on the bundled template, which already ran `migrate` and `firefly:cache` for you (via
`post-create-project-cmd`), so the app boots reflection-free and its sample `POST /orders` persists from the
first request; re-run `firefly:cache` whenever you add or change
`#[Component]`/`#[RestController]`/`#[CommandHandler]`/etc. classes. `firefly:clear` drops back to the
in-process scan, which costs a reflection pass per boot but is functionally identical — every manifest
resolves to the compiled artifact if present, otherwise a scan of `firefly.scan.paths`, otherwise empty.

`firefly:serve` is a thin passthrough to `artisan serve` (or `octane:start` when `laravel/octane` is
installed) — see [CLI](cli.md) for the full command reference.

## Adding LaraFly to an existing Laravel app

Install the complete framework library, including the CLI, dashboard and API documentation:

```bash
composer require fireflyframework/larafly
```

All component code is included. Optional adapters are activated by configuration and may require a PHP
extension. For the test kit, add `orchestra/testbench:^11.1` and `illuminate/testing:^13.0` with `--dev`.
Existing component requirements are satisfied by the root library's `replace` metadata.

Then point LaraFly at your app's classes and compile it. The one key an application must get right is
`firefly.scan.paths` — the PSR-4 roots every scanner walks; the skeleton's `config/firefly.php` ships it
pointed at `App\\`:

<!-- source: skeleton/config/firefly.php -->

```php
'scan' => [
    'paths' => [
        'App\\' => app_path(),
    ],
],
```

```bash
php artisan firefly:cache
php artisan firefly:serve
```

## The library and component requirement

`firefly/skeleton` is itself a `type: project` create-project template rather than something you require, and
its `composer.json` asks for the library and a CLI component requirement beside `php: ^8.3` and `laravel/framework: ^13.0`:

<!-- source: skeleton/composer.json -->

```json
"require": {
    "php": "^8.3",
    "firefly/cli": "*@dev",
    "fireflyframework/larafly": "*@dev",
    "laravel/framework": "^13.0"
},
```

- **`firefly/cli`** — the developer-experience console: `firefly:cache`/`:clear`, actuator-over-CLI
  `firefly:about`/`:routes`/`:health`/`:metrics`, `firefly:oauth2:keys`, the `make:firefly-*` generator
  family, and thin `firefly:serve`/`:schedule`/`:db` passthroughs. See [CLI](cli.md).
- **`fireflyframework/larafly`** — the complete `type: library` distribution. It supplies `firefly/cli` through
  `replace`, so both requirements resolve to the same installation. The template's CLI requirement
  explicitly records its use of `firefly:cache`.

The template is bundled in the library and supplied to Composer by `firefly new`; it does not need a
separate Packagist package. See [Publishing](publishing.md) for the transition from split packages.

## Where to next

- [Architecture](architecture.md) — how the boot engine, DI, and auto-configuration fit together.
- [Modules](modules.md) — the complete, grouped index of every module guide.
- [Auto-Configuration](modules/starters.md) — writing your own `#[Configuration]`/`#[Bean]` starters.
- [Testing](modules/testing.md) — the `firefly/testing` harness every package (and your app) dogfoods.

### The browser suite

The monorepo also drives the shipped skeleton app in a real Chromium, through `pestphp/pest-plugin-browser`
(Playwright), and that suite is **not** part of `composer check`. The `check` script composes exactly four
others — `pint-test`, `stan`, `test`, `deptrac` — and `test` runs the `unit` testsuite, which excludes
`tests/Browser`. The browser suite is its own testsuite and its own script, because the plugin starts
Playwright the moment a file under `tests/Browser/` is loaded, and requiring Node for `composer test` would
be a tax on every contributor who never touches a page:

```bash
npm ci
npx playwright install chromium   # once
composer test:browser
```

See [Contributing](contributing.md#browser-tests) for what each scenario proves.
