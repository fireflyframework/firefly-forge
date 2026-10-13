# Testing

`firefly/testing` is LaraFly's first-party test-support kit — the Spring `spring-boot-test`/`@SpringBootTest`
analogue. It ships a boot harness that mirrors the real production boot path (`FireflyAutoConfigureServiceProvider`
first, config seeded eager, filesystem-free logging), sqlite/testcontainers database bases, web/data "slice"
builders that boot only the beans a test needs, recording doubles for every capability package's port, a set of
Firefly-flavored Pest expectations, and a small fixture layer. It is dev-scoped — `require-dev` only, no runtime
service provider — and every other package in the monorepo dogfoods it for its own test suite.

## The boot harnesses

There are two families, matching the milestone's Family A / Family B split:

- **`FireflyTestCase`** (`Firefly\Testing\FireflyTestCase`) — a Testbench `TestCase` subclass. Subclass it and
  override three hooks; the base absorbs AutoConfigure-first provider ordering, the eager config seed, and a
  filesystem-free log channel:

    - `fireflyProviders(): array` — the capability service providers this test needs, in registration order.
      `FireflyAutoConfigureServiceProvider` is **always** prepended by the harness; do not list it yourself.
    - `configOverrides(): array` — eager, dot-keyed config seeded **before** boot, so `#[ConditionalOn*]` (which
      scan at register time) observe it. This is the seam for `firefly.scan.paths`, `firefly.<feature>.*` flags,
      etc.
    - `defineFireflyEnvironment(Application $app): void` — bindings/manifests set **after** config, e.g.
      `$app->instance(SomeManifest::class, ...)` or a port fake for the whole test class.

  <!-- illustrative: a test case a reader writes in their own application, over their own scan path -->
  ```php
  use Firefly\Testing\FireflyTestCase;

  final class WidgetProbeTest extends FireflyTestCase
  {
      protected function configOverrides(): array
      {
          return ['firefly.scan.paths' => [
              'App\\Widgets\\' => app_path('Widgets'),
          ]];
      }
  }
  ```

  `FireflyTestCase` also exposes `app(): Application` (throws a `LogicException` if called before boot, instead of
  returning `null` silently), `fireflyContext(): ApplicationContext` (the booted boot-engine facade), and
  `responseBody(TestResponse $response): string` (reads the body via `baseResponse->getContent()`, since
  `TestResponse::streamedContent()` throws on a non-streamed response).

- **`bootFireflyApp()` / `fireflyApplication()`** (`Firefly\Testing\functions.php`, autoloaded via
  Composer's `files` autoload) — the Family B analogue for bare, no-Testbench boots:

  <!-- source: packages/testing/src/functions.php -->
  ```php
  function fireflyApplication(array $config = [], array $providers = [], array $bindings = [], array $needs = []): Application
  // …
  function bootFireflyApp(array $config = [], array $providers = [], array $bindings = [], array $needs = []): ApplicationContext
  ```

  - `$config` — full config array (defaults to `['firefly' => []]`).
  - `$providers` — service providers registered **after** `FireflyAutoConfigureServiceProvider`, which is always
    registered first.
  - `$bindings` — `$app->instance($abstract, $instance)` overrides bound before provider registration (e.g. port
    doubles).
  - `$needs` — a "missing-bindings menu": any of `'cache'`, `'validation'`, `'http'` binds a fresh, isolated
    fallback (`ArrayStore`-backed cache, a real `IlluminateValidator`, a real HTTP kernel) for a capability a bare
    boot would otherwise lack — overridable by an explicit `$bindings` entry for the same abstract.

  `bootFireflyApp()` is the same call, but returns the booted `ApplicationContext` instead of the raw `Application`:

  <!-- source: packages/testing/tests/BootFireflyAppTest.php -->
  ```php
  it('boots a bare cqrs app with AutoConfigure first and an explicit binding', function () {
      $context = bootFireflyApp(
          config: ['firefly' => ['cqrs' => []]],
          providers: [CqrsServiceProvider::class, CqrsWiringProvider::class],
          bindings: [HandlerManifest::class => new HandlerManifest([], [])],
      );

      expect($context)->toBeInstanceOf(ApplicationContext::class)
          ->and($context->has(HandlerManifest::class))->toBeTrue();
  });
  ```

## Database test cases

- **`FireflyDatabaseTestCase`** (`Firefly\Testing\FireflyDatabaseTestCase`) — a `FireflyTestCase` that binds a
  shared sqlite `:memory:` connection as the `testing` default and adds `createSchema(string $table, Closure(Blueprint): void $blueprint): void`, a thin wrapper over `Schema::create()`:

  <!-- source: packages/testing/tests/Support/WidgetFireflyDatabaseTestCase.php -->
  ```php
  class WidgetFireflyDatabaseTestCase extends FireflyDatabaseTestCase
  {
      // …
      protected function defineFireflyEnvironment(Application $app): void
      {
          $this->createSchema('widgets', function (Blueprint $table): void {
              $table->id();
              $table->string('name');
          });
      }
  }
  ```

- **`UsesSqliteMemory`** (`Firefly\Testing\Concerns\UsesSqliteMemory`) — the trait `FireflyDatabaseTestCase` mixes
  in. Its one method, `defineSqliteMemory(Application $app): void`, sets `database.default` to `testing` and seeds
  `database.connections.testing` with the sqlite `:memory:` driver config. Mix it directly into your own base if
  you need the sqlite seed without the rest of `FireflyDatabaseTestCase`.

## Recording doubles

Every capability package's port gets a recording/stub double, so a test can assert on **what happened** without
Laravel's `Event::fake()`/`Bus::fake()` (which don't see Firefly's own ports):

| Port | Double (`Firefly\Testing\Double\*`) | Records |
|---|---|---|
| `Firefly\Eda\EventPublisher` | `RecordingEventPublisher` | `$published` — a list of `{destination, eventType, payload, headers}`; `publishedOf(string $eventType)` filters by type |
| `Firefly\Context\Event\ApplicationEventPublisher` | `RecordingApplicationEventPublisher` | `$events` — every published event object, in order; `ofType(string $class)` filters |
| `Firefly\Cqrs\Command\CommandBus` | `RecordingCommandBus` | `$sent`; `willReturn(string $commandClass, mixed $result)` programs a canned result; `handled(string $class)` filters |
| `Firefly\Cqrs\Query\QueryBus` | `StubQueryBus` | `$asked`; `willReturn(string $queryClass, mixed $result)` programs a canned result |
| `Firefly\Cqrs\Metrics\CqrsMetrics` | `RecordingCqrsMetrics` | `$commandSuccesses`/`$commandFailures`/`$querySuccesses`/`$queryFailures`, each `{command\|query, seconds}` |
| `Firefly\Cqrs\Event\CommandEventPublisher` | `RecordingCommandEventPublisher` | `$published` (list of `DomainEvent`); constructor takes an optional `?Throwable $throw` to exercise the bridge's failure path |
| `Firefly\Messaging\MessageBrokerPort` | `RecordingMessageBroker` | `$published` — a list of `{topic, value, key, headers}`; `publishedTo(string $topic)` filters |
| `Firefly\Scheduling\Lock\DistributedLock` | `RecordingDistributedLock` | `$acquired`/`$released`; constructor `(bool $available = true)`; `setAvailable(bool $available)` toggles whether `tryAcquire()` grants the lock |
| `Firefly\Actuator\Health\HealthIndicator` | `FakeHealthIndicator` | Defaults `Status::Up`; constructor takes a `Health`, `setHealth(Health $health)` reprograms it for DOWN/OUT_OF_SERVICE scenarios |
| `Firefly\Observability\Tracing\Tracer` | `RecordingTracer` | The whole port in memory with real W3C-shaped ids: `recorded()` (list of `RecordedSpan` — `name`, `kind`, `attributes`, `events`, `status`, `exception`, `parent`, `ended`), `find(name)`, `ofKind(kind)`, `reset()`; `$spans` — every span name, in start order |

<!-- source: packages/testing/tests/Double/RecordingEventPublisherTest.php -->
```php
it('records publishes in the FakeEventPublisher-compatible shape', function () {
    $publisher = new RecordingEventPublisher;
    $publisher->publish('accounts.events', 'AccountOpened', ['owner' => 'alice'], ['x' => '1']);

    expect($publisher->published)->toHaveCount(1)
        ->and($publisher->published[0]['destination'])->toBe('accounts.events')
        ->and($publisher->published[0]['eventType'])->toBe('AccountOpened')
        ->and($publisher->published[0]['payload']['owner'])->toBe('alice')
        ->and($publisher->published[0]['headers']['x'])->toBe('1');
});
```

## Security test support

`FireflyTestCase` carries Spring Security's test module as methods: `actingAsPrincipal()` (a principal for direct calls
and every HTTP request), `actingAsOidcUser()` (a real `OidcUser` with claims, scopes, extra authorities and a registration
id — no provider involved), `actingAsAuthentication()` (any `Authentication`, the seam beneath both), `withoutSecurity()`,
and `#[WithMockUser]`; `RecordingAuthenticationEvents` records the security event family. See
[Security → Testing](security.md#testing) and [OAuth2 Client → Testing](security-oauth2-client.md#testing), where
`Firefly\Testing\Security\OAuth2\FakeAuthorizationServer` — an OpenID Connect provider in one class, real front-channel
routes and a faked back channel — is described.
## The OAuth2 authorization server: `OAuth2ServerTestClient`

`Firefly\Testing\Security\OAuth2\OAuth2ServerTestClient` drives an application's own
[authorization server](security-oauth2-server.md#testing) through the test client: `authorize()` (a fresh PKCE
pair and state), `approveConsent()`, `obtainCode()`, `exchangeCode()`, `clientCredentials()`, `refresh()`,
`introspect()`, `revoke()`, `userInfo()` and `tokens()`, with the endpoint paths read from the
`AuthorizationServerSettings` bean. Sign the user in through the login page first and carry the session cookie:
the authorization endpoint answers a browser and reads the principal the `SecurityContextRepository` holds
between requests, so `actingAsPrincipal()` — a principal in the holder for one request — is answered with the
login redirect, exactly as an anonymous browser is. The token, introspection, revocation and userinfo helpers are
machine endpoints and need no sign-in.

## Feature flag overrides

`withFeatureFlags(array $flags)` from `Firefly\Testing\functions.php` overlays definitions in the running
application for the rest of the test. It accepts shorthand or full flagd definitions, stays in this process,
and is neither stored nor served by the sync endpoint. The returned `FeatureFlagOverrides` supports `set()`,
`forget()`, `merge()` and `clear()`. Enable `firefly.feature-flags.enabled` in the test configuration first.

<!-- illustrative: a Pest test in an application's own suite -->
```php
it('hides the beta report while its flag is off', function (): void {
    withFeatureFlags(['beta-reports' => false]);

    $this->get('/reports/beta')->assertNotFound();
});
```

## Pest expectations

Registered once, at monorepo boot, by `Firefly\Testing\Pest\FireflyExpectations::register()` — called from the
root `tests/Pest.php`, the only Pest bootstrap file the monorepo loads:

<!-- source: tests/Pest.php -->
```php
use Firefly\Testing\Pest\FireflyExpectations;
// …
FireflyExpectations::register();
```

- **`toHavePublished(string $eventType, array $payloadContains = [])`** — on a `RecordingEventPublisher`; asserts at
  least one published event of the given type whose payload contains the given key/value subset.
- **`toHaveHandledCommand(string $class)`** — on a `RecordingCommandBus`; asserts at least one sent command is an
  instance of `$class`.
- **`toBeUp()`** — on a `Health` or a `HealthIndicator` (narrowed via `->health()` if needed); asserts
  `Status::Up`.
- **`toHaveRecordedMetric(string $name, array $tags = [])`** — on anything exposing `meters(): list<Meter>` (e.g. a
  `MeterRegistry`); asserts at least one recorded meter with the given name and tag subset.
- **`toBeProblemDetails(int $status)`** — on a `TestResponse` (checks the `application/problem+json` content type
  and decodes the JSON body) or a plain array; asserts the RFC-7807 shape has `type`/`title`/`status` keys and that
  `status` matches.

The package's own suite is where each of them is exercised; this is `toHavePublished()` with a payload subset:

<!-- source: packages/testing/tests/Double/RecordingEventPublisherTest.php -->
```php
it('supports the toHavePublished expectation with a payload subset', function () {
    $publisher = new RecordingEventPublisher;
    $publisher->publish('accounts.events', 'AccountOpened', ['owner' => 'alice', 'balance' => 500]);

    // toHavePublished() is registered at runtime by FireflyExpectations::register() (tests/Pest.php),
    // invisible to PHPStan's static reflection of the vendor Pest\Expectation class — see the matching
    // note in functions.php's assertEventPublished().
    // @phpstan-ignore method.notFound
    expect($publisher)->toHavePublished('AccountOpened', payloadContains: ['owner' => 'alice']);
});
```

> **Note on the `@phpstan-ignore method.notFound` comments:** Pest registers `expect()->extend(...)` custom
> expectations at *runtime*. PHPStan's static reflection of the vendor `Pest\Expectation` class has no way to see
> a method added this way, so it flags every call as `method.notFound` even though the method genuinely exists at
> runtime (proven by the package's own test suite). Keep each custom expectation on its own `expect()` statement
> rather than chaining with `->and(...)` — chaining degrades the PHPStan error from the precisely-ignorable
> `method.notFound` to the unignorable `method.nonObject`.

For plain PHPUnit-style assertions instead of the fluent expectations, two procedural helpers are available
alongside `bootFireflyApp()`/`fireflyApplication()` in `Firefly\Testing\functions.php`:

<!-- source: packages/testing/src/functions.php -->
```php
function assertEventPublished(object $publisher, string $eventType, array $payloadContains = []): void
// …
function assertNoEventsPublished(object $publisher): void
```

<!-- source: packages/testing/tests/Double/RecordingEventPublisherTest.php -->
```php
it('supports the procedural assertions', function () {
    $publisher = new RecordingEventPublisher;
    assertNoEventsPublished($publisher);

    $publisher->publish('d', 'Thing', ['id' => 7]);
    assertEventPublished($publisher, 'Thing', ['id' => 7]);
});
```

## Slice builders

Two "slice" bases boot **only** the beans a PSR-4 scan discovers, plus explicit port overrides — the Spring
`@WebMvcTest`/`@DataJpaTest` analogues:

- **`WebSliceTestCase`** (`Firefly\Testing\Slice\WebSliceTestCase`, extends `FireflyTestCase`) — boots the real
  Web + Validation pipeline (including `RouteWiringPass`) over only the sliced controllers, so `$this->get(...)`
  serves a real `TestResponse` against them.
- **`DataSliceTestCase`** (`Firefly\Testing\Slice\DataSliceTestCase`, extends `FireflyDatabaseTestCase`) — boots
  the real Data pipeline over only the sliced beans, then fail-fast resolves every scanned bean so a mis-wired
  slice fails immediately instead of silently.

Both expose a **method**, callable from a Pest closure test:

<!-- source: packages/testing/src/Slice/WebSliceTestCase.php -->
```php
public function webSlice(array $scan = [], array $overrides = []): ApplicationContext
```

<!-- source: packages/testing/src/Slice/DataSliceTestCase.php -->
```php
public function dataSlice(array $scan = [], array $overrides = []): ApplicationContext
```

<!-- source: packages/testing/tests/Slice/WebSliceTest.php -->
```php
use Firefly\Testing\Slice\WebSliceTestCase;

uses(WebSliceTestCase::class);

it('boots a web slice and serves a sliced controller route', function () {
    /** @var WebSliceTestCase $this */
    $this->webSlice(scan: [
        'Firefly\\Testing\\Tests\\Fixtures\\Slice\\' => dirname(__DIR__).'/Fixtures/Slice',
    ]);

    $response = $this->get('/slice/ping');

    expect($response->status())->toBe(200)
        ->and($response->json('pong'))->toBeTrue();
});
```

<!-- source: packages/testing/tests/Slice/DataSliceTest.php -->
```php
uses(DataSliceTestCase::class);

it('boots ONLY the sliced data beans and resolves them', function () {
    /** @var DataSliceTestCase $this */
    $context = $this->dataSlice(
        scan: [
            'Firefly\\Testing\\Tests\\Fixtures\\Slice\\' => dirname(__DIR__).'/Fixtures/Slice',
            'Firefly\\Testing\\Tests\\Fixtures\\SliceData\\' => dirname(__DIR__).'/Fixtures/SliceData',
        ],
        overrides: [
            PricingPort::class => new class implements PricingPort
            {
                public function price(): int
                {
                    return 42;
                }
            },
        ],
    );
    // …
});
```

Both `webSlice()`/`dataSlice()` are **per-test-class**: calling one a second time with different arguments throws
a `LogicException`.

### `#[FireflyTest]` / `#[WebSlice]` / `#[DataSlice]` attributes

`Firefly\Testing\Attributes\FireflyTest`, `WebSlice`, and `DataSlice` are **class-target attributes** — the
analog of the three bases/methods above for **hand-written, class-style tests** (a PHPUnit-style test class, not a
Pest closure file):

<!-- source: packages/testing/src/Attributes/FireflyTest.php -->
```php
    public function __construct(
        public array $providers = [],
        public array $config = [],
    ) {}
```

`#[WebSlice]` and `#[DataSlice]` share the other shape:

<!-- source: packages/testing/src/Attributes/WebSlice.php -->
```php
    public function __construct(
        public array $scan = [],
        public array $overrides = [],
    ) {}
```

The monorepo's one class-style test is exactly this shape:

<!-- source: packages/testing/tests/Attributes/SliceAttributesTest.php -->
```php
#[WebSlice(scan: ['Firefly\\Testing\\Tests\\Fixtures\\Slice\\' => __DIR__.'/../Fixtures/Slice'])]
final class SliceAttributesTest extends WebSliceTestCase
{
    public function test_web_slice_attribute_boots_the_sliced_route(): void
    {
        $response = $this->get('/slice/ping');

        $this->assertSame(200, $response->status());
        $this->assertTrue($response->json('pong'));
        $this->assertInstanceOf(SliceController::class, $this->fireflyContext()->get(SliceController::class));
    }
}
```

> **Attributes vs. the method calls — when to use which.** `#[FireflyTest]`/`#[WebSlice]`/`#[DataSlice]` are read
> by the base class's `setUp()` **before** the (one-and-only) Testbench boot runs, so they only work on a
> hand-written **class extending `FireflyTestCase`/`WebSliceTestCase`/`DataSliceTestCase`** — not on a Pest
> closure file. Pest closure tests (`it('...', function () { ... })`) call `$this->webSlice(...)` /
> `$this->dataSlice(...)` directly instead: the method internally calls `refreshApplication()` to re-boot with the
> slice values, since testbench already performed an empty first boot in `setUp()` before the closure body ever
> runs. Do not mix the two on the same test class.

## Fixture layer

- **`FixtureRegistry`** (`Firefly\Testing\Fixture\FixtureRegistry`) — a thin named-fixture registry over plain
  factory closures, the Firefly convenience atop Eloquent factories:

  <!-- source: packages/testing/src/Fixture/FixtureRegistry.php -->
  ```php
      public function register(string $name, Closure $factory): self
      // …
      public function has(string $name): bool
      // …
      public function make(string $name, array $overrides = []): object
      // …
      public function load(string ...$names): array
  ```

  <!-- source: packages/testing/tests/Fixture/FixtureLayerTest.php -->
  ```php
  it('registers, makes with overrides, and loads named fixtures', function () {
      $registry = (new FixtureRegistry)
          ->register('widget', function (array $overrides): Widget {
              $name = $overrides['name'] ?? 'default';
              $qty = $overrides['qty'] ?? 1;

              return new Widget(
                  is_string($name) ? $name : 'default',
                  is_int($qty) ? $qty : 1,
              );
          });
      // …
      expect($registry->has('widget'))->toBeTrue()
          ->and($default->name)->toBe('default')
          ->and($bolt->qty)->toBe(5)
          ->and($registry->load('widget', 'widget'))->toHaveCount(2);
  });
  ```

- **`AggregateSeeder`** (`Firefly\Testing\Fixture\AggregateSeeder`) — replays an aggregate's domain events through
  an `ApplicationEventPublisher` port:

  <!-- source: packages/testing/src/Fixture/AggregateSeeder.php -->
  ```php
      public function publishEvents(ApplicationEventPublisher $publisher, object ...$events): void
  ```

  <!-- source: packages/testing/tests/Fixture/FixtureLayerTest.php -->
  ```php
      $publisher = new RecordingApplicationEventPublisher;
      (new AggregateSeeder)->publishEvents($publisher, new Widget('a'), new Widget('b'));

      expect($publisher->events)->toHaveCount(2);
  ```

- **`ListenerSpy`** (`Firefly\Testing\Fixture\ListenerSpy`) — records arbitrary string values a listener/handler
  fixture saw, in order. Replaces the per-package hand-rolled `Spy` classes:

  <!-- source: packages/testing/src/Fixture/ListenerSpy.php -->
  ```php
      public array $seen = [];
      // …
      public function record(string $value): void
  ```

  <!-- source: packages/testing/tests/Fixture/ListenerSpyTest.php -->
  ```php
  it('appends every recorded value to $seen, in order', function () {
      $spy = new ListenerSpy;

      $spy->record('order.placed');
      $spy->record('order.shipped');

      expect($spy->seen)->toBe(['order.placed', 'order.shipped']);
  });
  ```

## Boot-time footgun-killers: `FireflyBoot`

`Firefly\Testing\Boot\FireflyBoot` centralizes two boot-time gotchas the hand-rolled test bases used to
re-derive by hand:

<!-- source: packages/testing/src/Boot/FireflyBoot.php -->
```php
    public static function makeConditionEvaluator(Config $config, Profiles $profiles = new Profiles([])): ConditionEvaluator
    // …
    public static function stubScheduledManifest(Application $app): ScheduledManifest
```

- **`makeConditionEvaluator()`** — `ConditionEvaluator`'s constructor is arity-2 (`Config` + `Profiles`), but tests
  that never exercise `#[ConditionalOnProfile]` tend to forget the second argument. This helper defaults
  `$profiles` to an empty `Profiles([])` so the common case stays a 1-arg call.
- **`stubScheduledManifest()`** — the actuator/observability `ScheduledTasksEndpoint` singleton resolves a
  `ScheduledManifest` eagerly during container boot, even in slices that never touch scheduling. This binds +
  returns the canonical empty `new ScheduledManifest([])` stub so such a slice can still boot.

<!-- illustrative: the two calls a reader makes from their own test's setUp -->
```php
use Firefly\Config\Config;
use Firefly\Testing\Boot\FireflyBoot;
use Illuminate\Config\Repository;
use Illuminate\Foundation\Application;

$config = new Config(new Repository(['firefly' => ['demo' => ['enabled' => true]]]));
$evaluator = FireflyBoot::makeConditionEvaluator($config);

$app = new Application;
$manifest = FireflyBoot::stubScheduledManifest($app);
```

## Testcontainers hook

For real-backend integration tests (Postgres, Kafka, etc.), see
[Integration Testing](integration-testing.md) — `firefly/testing` ships `RequiresDocker` +
`fireflyConfigFor()` to make Docker-backed tests opt-in and CI-safe.
