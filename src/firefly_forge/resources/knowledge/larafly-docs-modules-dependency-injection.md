# Dependency Injection

`firefly/container` is LaraFly's dependency-injection layer over `Illuminate\Container`. You annotate classes
with PHP 8 attributes; a component scan compiles them to a cached manifest; the container resolves them by
type, by interface, by name, or as ordered lists.

## Stereotypes

Mark a class as a managed component with a stereotype attribute:

<!-- illustrative: an application's own service class, which by definition is not a file in this repository -->
```php
use Firefly\Container\Attributes\Service;

#[Service]
final class OrderService {}
```

`#[Service]`, `#[Repository]`, and `#[Configuration]` are all specialisations of `#[Component]`; a scan finds
them via `ReflectionClass::getAttributes(Component::class, ReflectionAttribute::IS_INSTANCEOF)`.

## Scopes

Every component is a singleton by default. Choose another lifetime with the `scope` argument:

<!-- illustrative: an application's own transient-scoped bean -->
```php
use Firefly\Container\Attributes\Service;
use Firefly\Container\Scope;

#[Service(scope: Scope::Transient)]  // a new instance per resolution
final class RequestId {}
```

`Scope::Scoped` binds one instance per Laravel scope/request. (`Session` scope arrives with `firefly/session`.)

## Interfaces, `#[Primary]`, and `#[Qualifier]`

When several components implement one interface, the container binds the interface to the `#[Primary]` one, and
you can fetch any specific implementation by name:

<!-- illustrative: an application's own interface and its two implementations -->
```php
use Firefly\Container\Attributes\{Primary, Qualifier, Service};

interface Greeter { public function greet(): string; }

#[Service] #[Primary]
final class EnglishGreeter implements Greeter { public function greet(): string { return 'Hello'; } }

#[Service('spanish')] #[Qualifier('spanish')]
final class SpanishGreeter implements Greeter { public function greet(): string { return 'Hola'; } }
```

<!-- illustrative: the three calls a reader makes against the container port from their own code -->
```php
$container->get(Greeter::class);         // EnglishGreeter (primary)
$container->getByName('spanish');        // SpanishGreeter
$container->getAll(Greeter::class);      // all implementations, sorted by #[Order]
```

`#[Qualifier]` also works **on an injected parameter**, which is how you ask for a specific bean without
going through `getByName()`:

<!-- illustrative: an application bean asking for one named implementation by qualifier -->
```php
final class Notifier
{
    public function __construct(
        #[Qualifier('spanish')] private readonly Greeter $greeter,
    ) {}
}
```

It rides `Illuminate\Contracts\Container\ContextualAttribute`, the same seam `#[Value]` uses, so it adds
no reflection that was not already happening and leaves the compiled manifest shape untouched. It works on
`#[Bean]` factory-method parameters as well as constructors. A name that is not registered throws a
`ConfigurationException` naming the qualifier — it does not fall back to the type. (Parameter qualifiers were
declared but read by nothing until recently: `#[Qualifier('redisCache')] Cache $cache` silently received
whatever `Cache::class` resolved to.)

## `#[Order]`

`#[Order]` sets list precedence (lower first, Spring convention). `getAll()` returns implementations sorted by it.

## `#[Lazy]` (reserved)

`#[Lazy]` is accepted on components for forward-compatibility but is currently a **no-op**: `Illuminate\Container`
already resolves bindings lazily by default, so there is nothing extra to defer today. Explicit-lazy semantics
(e.g. proxying construction) are reserved for a future milestone.

## `#[Bean]` factory methods

A component exposes `#[Bean]` methods; each is registered under its return type, with parameters injected:

<!-- source: packages/container/tests/Fixtures/AppConfig.php -->
```php
use Firefly\Container\Attributes\Bean;
use Firefly\Container\Attributes\Configuration;

#[Configuration]
final class AppConfig
{
    #[Bean('utcClock')]
    public function clock(): Clock
    {
        return new Clock('UTC');
    }
}
```

`#[Bean]` methods are collected from **any** component — `#[Configuration]`, a user-defined stereotype that
extends it, and plain `#[Component]`/`#[Service]`/`#[Repository]` classes (Spring's "lite mode"). Discovery
uses the same `IS_INSTANCEOF` rule as every other stereotype check; it does not compare attribute short
names, which used to make `#[Bean]` methods on an `ApiConfiguration extends Configuration` disappear from the
manifest while the class itself was still bound.

### Two or more `#[Bean]` methods returning the same type

!!! warning "Breaking change"
    Two non-`#[Primary]` `#[Bean]` methods returning the same type now **throw at registration**. They
    previously booted, and one of the two beans silently did not exist.

Per return type:

- **One bean produces the type** (the common case): unchanged. The factory is bound on the return type, and
  the name, if any, is aliased to it — the type and the name resolve to the same singleton.
- **Several beans produce the type**: each is bound under its **own name key**, so every one is individually
  resolvable, and the bare type key becomes an **alias** of the `#[Primary]` winner. An alias, never a second
  binding — a second binding of the same factory would quietly mint a second "singleton".
- **Several beans, no `#[Primary]`**: the type key is bound to a guard that throws a `ConfigurationException`
  naming every candidate. The type stays *bound*, so `#[ConditionalOnMissingBean]` still sees that a bean of
  that type exists; leaving it unbound would let a concrete return type silently auto-wire past every
  `#[Bean]` factory.

Four shapes are rejected at registration time, where the stack trace still points at the manifest rather
than at some unlucky consumer:

| Rejected | Why |
|---|---|
| Competing beans where one or more is **anonymous** | An unnamed bean is reachable only through its return type, which its competitors already claim — it could never be resolved. |
| Competing beans **sharing one name** | A bean name is a container key; the second would silently overwrite the first. |
| Competing beans where one is **named after the type itself** | That name *is* the group's type key, claimed by the `#[Primary]` winner or the guard. |
| **More than one `#[Primary]`** for a type | `#[Primary]` names the single default; at most one candidate may carry it. |

What this replaces: names were previously only ever recorded as `alias($returns, $name)`, and an alias is a
pointer to a key rather than a binding of its own. Two `#[Bean]` methods returning the same type therefore
collapsed — both names pointed at the one type key, which held whichever factory registered last, so
`getByName('memoryCache')` and `getByName('redisCache')` handed back the identical object. `#[Primary]` could
not break the tie because it was read nowhere in the bean path at all.

**To migrate**, give each competing method a distinct name and mark one primary — which is exactly the shape of
the package's own regression fixture:

<!-- source: packages/container/tests/Fixtures/CacheConfig.php -->
```php
#[Configuration]
final class CacheConfig
{
    #[Bean('memoryCache')]
    #[Primary]
    public function memory(): Cache
    {
        return new MemoryCache;
    }

    #[Bean('redisCache')]
    public function redis(): Cache
    {
        return new RedisCache;
    }
}
```

## `#[Value]` injection

Inject configuration and expressions into constructor parameters:

<!-- illustrative: an application's own bean reading an environment variable and an expression -->
```php
use Firefly\Container\Attributes\Value;

final class MailerConfig
{
    public function __construct(
        #[Value('${MAIL_HOST:localhost}')] public readonly string $host,
        #[Value('#{25 * 2}')] public readonly int $port,
    ) {}
}
```

`${NAME:default}` reads an environment variable (falling back to the default); `#{expr}` evaluates a sandboxed
expression. `firefly/config` extends resolution to read application configuration.

## Caching (Octane-safe)

The component scan is compiled once to a manifest and never runs per request. At runtime the container is booted
from the frozen manifest — no reflection on the hot path — which keeps it fast and safe under Octane.
