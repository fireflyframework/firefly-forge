# Web Layer

`firefly/web` is LaraFly's HTTP layer: `#[RestController]`/`#[Controller]` routing compiled to a
`RouteManifest`, parameter binding with `#[Valid]` interception, content negotiation (JSON for data, HTML
for views), and RFC-7807 error rendering — all dispatched through native Laravel routes, inside the real
HTTP-kernel middleware pipeline.

![Request lifecycle](../assets/diagrams/request-lifecycle.svg)

## `#[RestController]`

`#[RestController]` is a `#[Component]` stereotype (it extends `Firefly\Container\Attributes\Component`),
so a controller is discovered by the same component scan as any other bean and gets constructor
dependency injection for free — no separate controller-registration mechanism. This is the package's own
dispatch fixture — every binding attribute on one controller, with the imports and the local
`#[ExceptionHandler]` cut:

<!-- source: packages/web/tests/Fixtures/AccountsController.php -->
```php
#[RestController]
#[RequestMapping('/accounts')]
final class AccountsController
{
    /** @return array<string,mixed> */
    #[GetMapping('/{id}', name: 'accounts.show')]
    public function show(
        #[PathVariable] int $id,
        #[QueryParam(default: 'summary')] string $view,
        #[RequestHeader('X-Trace')] ?string $trace = null,
    ): array {
        return ['id' => $id, 'view' => $view, 'trace' => $trace];
    }

    /** @return array<string,mixed> */
    #[PostMapping(status: 201)]
    public function create(#[Valid] #[RequestBody] CreateAccountRequest $body): array
    {
        return ['iban' => $body->iban, 'owner' => $body->owner];
    }
    // …
}
```

The `RouteScanner` (routing metadata) and the component scan (DI wiring) are two separate passes over
the same class — a `#[RestController]` never has to declare its own route registration.

## `#[Controller]` — the HTML stereotype

`#[Controller]` is to `#[RestController]` what Spring's `@Controller` is to `@RestController`: same routing,
different intent. It **extends** `#[RestController]`, so `RouteScanner`'s `IS_INSTANCEOF` filter finds it with
no scanner change, its routes compile into the same `RouteManifest`, and constructor DI is identical. What
differs is what the method returns and how the response is built.

<!-- illustrative: an application's own HTML controller; the skeleton ships a real #[Controller] at skeleton/app/Http/WelcomeController.php, but its index() renders the whole welcome page rather than these two teaching lines -->
```php
use Firefly\Web\Attributes\{Controller, GetMapping};
use Firefly\Web\View\ModelAndView;
use Illuminate\Contracts\View\View;

#[Controller]
final class WelcomeController
{
    #[GetMapping('/', name: 'welcome')]
    public function index(): View
    {
        return view('welcome', ['name' => 'Ada']);   // rendered as text/html
    }

    #[GetMapping('/about')]
    public function about(): ModelAndView
    {
        return ModelAndView::of('about', ['version' => '1.0'])->withStatus(200);
    }
}
```

`ModelAndView` is a view **name** plus its model, resolved through the application's view factory. It exists
for a handler that should not reach for the `view()` helper — one under test, or one in a package that must
not depend on `illuminate/view` — and carries `of()`, `withModel()`, `withStatus()` and `withHeader()`. If no
view factory is bound, returning one fails loudly rather than rendering nothing.

Two deliberate boundaries:

- **A bare `string` return is *not* a view name.** `#[RestController]` methods legitimately return strings
  that must negotiate to JSON, and the meaning of a return value must not depend on the class that declares
  it. Explicit beats magic.
- **A `#[Controller]` may still return an array or a DTO**, which negotiates to JSON exactly as before — the
  same latitude Spring gives a `@Controller` method carrying `@ResponseBody`.

## `#[RequestMapping]` and the verb mappings

`#[RequestMapping(path: '...')]` is class-level and prepends a base path to every method mapping on the
controller. Each method carries exactly one verb attribute, all implementing the `Mapping` marker
interface so the scanner discovers them polymorphically (`IS_INSTANCEOF`) with no per-verb scanner code:

| Attribute | HTTP method |
|---|---|
| `#[GetMapping]` | GET |
| `#[PostMapping]` | POST |
| `#[PutMapping]` | PUT |
| `#[PatchMapping]` | PATCH |
| `#[DeleteMapping]` | DELETE |

Each verb attribute accepts `path` (relative, joined to the class-level base), `status` (the default
response status — 200 for the read verbs, customizable e.g. `status: 201` for `#[PostMapping]`), and
`name` (an optional Laravel route name).

## Parameter binding

Each controller-method parameter is bound by exactly one of these attributes, plus two binding
conventions that need no attribute at all:

| Attribute | Source | Notes |
|---|---|---|
| `#[PathVariable(name?, pattern?, notFoundCode?, notFoundMessage?)]` | route segment | always required; defaults to the parameter name. `pattern` (a PCRE body, anchored to the whole segment, case-insensitive) is checked **before** the handler runs and a miss answers `ResourceNotFoundException` with `notFoundCode` (default `RESOURCE_NOT_FOUND`) and `notFoundMessage` (default derived from the name: `roomId` → "That room does not exist."). `PathVariable::UUID` is the RFC 4122 shape. A malformed id is a 404, not a 400, so the wire cannot tell "no such row" from "not even an id" — the right property under row-level security. |
| `#[QueryParam(name?, default?, required?)]` | query string | not required by default |
| `#[RequestBody]` | request body | reads/decodes via the negotiated `MessageConverter`; combine with `#[Valid]` to gate on `BeanValidator` |
| `#[RequestHeader(name?, default?)]` | HTTP header | not required |
| `#[UploadedFile(name?)]` | `multipart/form-data` file | binds a framework-neutral `Firefly\Web\Http\UploadedFile` (filename/mimeType/size + lazy `contents()`/`store()`) |

Two parameters need no attribute at all:

- A **typed class parameter** (e.g. a service interface) with no binding attribute resolves as a
  **container service** — `$container->make($type)` — so a controller method can pull an extra
  collaborator straight from the container without adding it to the constructor.
- A **scalar parameter** with no binding attribute defaults to a **query parameter** keyed by the
  parameter's own name, required unless the parameter itself is optional.

Path and query values are coerced to the parameter's declared scalar type (`int`/`float`/`bool`); an
uncoercible value or a missing required input raises `InvalidRequestException` (HTTP 400,
`MISSING_PARAMETER` / `TYPE_CONVERSION_ERROR`), rendered like any other `FireflyException` — see
[Error Handling](error-handling.md).

## `#[Valid]` → `BeanValidator` → 422

Adding `#[Valid]` to a `#[RequestBody]` parameter gates the DTO on the Bean-Validation constraint layer
**before** the DTO is hydrated: the raw decoded body is validated by `Firefly\Validation\Constraint\BeanValidator`
against the DTO class's compiled rules, and only on success is the DTO constructed (from the raw body,
not the validator's `validated()` subset, so an unconstrained property is never silently dropped). A
failure throws the kernel's `ValidationException` — HTTP 422, rendered as `application/problem+json`
with one `FieldError` per violated constraint.

Constraints are per-property attributes on the DTO's constructor-promoted parameters (or plain typed
properties): `#[NotBlank]`, `#[NotEmpty]`, `#[NotNull]`, `#[AssertTrue]`/`#[AssertFalse]`, `#[Size]`,
`#[Min]`/`#[Max]`, `#[Digits]`, `#[Pattern]`, `#[Past]`/`#[Future]`, `#[Email]`, plus 16 financial-domain
wrappers (`#[Iban]`, `#[Bic]`, `#[Swift]`, `#[Isin]`, `#[Cusip]`, `#[RoutingNumber]`, `#[Luhn]`,
`#[CurrencyCode]`, `#[CountryCode]`, `#[LanguageTag]`, `#[UuidValue]`, `#[Phone]`, `#[PostalCode]`,
`#[Percentage]`, `#[Money]`, `#[DecimalScale]`) and a `#[Rules]` escape hatch for any raw Illuminate rule
string or `ValidationRule` object. `#[Valid]` on a nested DTO property cascades into it (dot-prefixed keys,
`shipTo.postcode`) and on an `array` property into every **element** (`lines[1].sku`) — the element class read
from `@param list<X>` / `@var list<X>` or `#[Valid(each: X::class)]`, the same source the hydrator builds
from — with a cycle guard against self-referential DTOs. Each field error is worded by the constraint
(`must not be blank`) and names it (`"constraint": "NotBlank"`). See [Validation](validation.md) for the full
constraint catalogue, the sentences, and the underlying `Rule` objects.

<!-- source: packages/web/tests/Fixtures/CreateAccountRequest.php -->
```php
final class CreateAccountRequest
{
    public function __construct(
        #[NotBlank]
        #[Iban]
        public readonly string $iban,
        #[NotBlank]
        public readonly string $owner,
    ) {}
}
```

## Content negotiation

`ResponseFactory` decides what to do with a controller's return value in this order:

| Return value | Response |
|---|---|
| A Symfony or Illuminate `Response` (including `JsonResponse`) | passed through untouched |
| A `Responsable` | `toResponse($request)` |
| A `ModelAndView` | resolved through the view factory, rendered `text/html; charset=UTF-8` |
| A `View` or any `Renderable` | `render()`, rendered as `text/html; charset=UTF-8` |
| An `Htmlable` | `toHtml()`, rendered as `text/html; charset=UTF-8` |
| Anything else (array, `JsonSerializable`, `Arrayable`, scalar) | written by the negotiated `MessageConverter` |

The HTML arms are why a server-rendered page is possible at all. Before they existed, a Blade `View` was
neither a `SymfonyResponse` nor a `Responsable`, so it fell through to the converter chain and was
`json_encode`d — and because a `View` exposes no public properties, **every returned view became the body
`{}` with HTTP 200 and `Content-Type: application/json`**, silently.

Data negotiation is JSON-native: the only shipped `MessageConverter` is `JsonMessageConverter`
(`application/json` and any `+json` suffix type). The converter is chosen from the request's `Accept`
header — parsed for q-values with a header-order tiebreak — falling back to the first (JSON) converter when
nothing matches or `Accept` is absent. Request bodies are read the same way, keyed off `Content-Type`.

`MessageConverterRegistry` is an ordinary container binding (guarded `#[ConditionalOnMissingBean]`-style
via `if (! $app->bound(...))`), so an application can register additional `MessageConverter`s — XML
support is deferred to this seam rather than shipped in M6 (tracked for SP-6).

## The compiled `RouteManifest`

`RouteScanner` — the one reflection site in `packages/web/src` — walks `#[RestController]` classes and
compiles each verb-mapped method into a `RouteDescriptor` (HTTP method, joined path, controller/method,
default status, optional route name, and a pure-array parameter-binding plan). `RouteManifestCompiler`
`var_export`s the descriptors into a require-able PHP file; `RouteManifest::load()` reads it back
reflection-free. At boot, `RouteWiringPass` (phase `WiringPasses`) registers one native Laravel route per
descriptor and hands each one a dispatch closure (`ControllerDispatcher`) — so route listing, URL
generation, and Laravel's own route-caching machinery all apply to LaraFly routes unmodified.

!!! note "Known-latent: manifest compilation, config ordering, and negotiation scope"
    - **App manifests need no hand-wiring.** `RouteManifest`, `ConstraintManifest` and
      `ExceptionHandlerRegistry` are each resolved through `Firefly\Context\Scan\AppScan`: the artifact
      `firefly:cache` compiled if it exists, otherwise an in-process scan of `firefly.scan.paths`, otherwise
      empty. An application therefore never has to compile and bind these itself — it did have to before the
      scan fallback existed, and until then an uncached app 404'd every route it owned.
    - **`route:cache` interplay.** Because dispatch runs through ordinary native Laravel routes, Laravel's
      own `route:cache` works unmodified once those routes are registered — there is no separate LaraFly
      route cache to keep in sync with it.
    - **Component-scan path ordering.** Auto-configuration discovers `#[Component]` beans — including
      `WebFilter`s — from a component scan whose paths are read at **provider-register time**. Any
      application config that sets `firefly.scan.paths` must therefore already be loaded *before*
      providers register. This is exactly the ordering Laravel itself guarantees (`LoadConfiguration`
      runs before `RegisterProviders`), so a config file is the correct place for this key — but a
      programmatic override made after providers have registered is too late to affect the scan.
    - **Error negotiation covers HTML and problem+json.** Named HTML receives the error page; JSON clients
      and configured `json-paths` receive the problem document. With `firefly.web.error-page.problem-fallback`
      enabled, other clients (wildcard, absent, XML or plain-text Accept) also receive problem+json. Laravel's
      own validation, authentication and carried-response exceptions remain with its handler. Error
      renderers do not use controller `MessageConverter`s. See [error handling](error-handling.md).
    - **Nested lists are not cascaded.** `#[Valid]` descends through nested DTOs and into the elements of a
      list, but not into a list of lists (`list<list<X>>`); the scanner refuses such a member at cache time
      rather than validating half of it.
    - **XML content negotiation is deferred** to the `MessageConverter` seam described above (SP-6);
      only JSON ships in M6.

---

Apache-2.0 © Firefly Software Solutions Inc.
