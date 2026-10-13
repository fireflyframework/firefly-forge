# Relational Data

`EloquentRepository` is the Eloquent-backed base every concrete repository extends: it implements the full
[`CrudRepository`/`PagingAndSortingRepository`](data.md#the-ports-crudrepository-pagingandsortingrepository)
contract over `Model::query()`, so an application repository is usually just a `$model` assignment plus its
own derived-query/`#[Query]` methods. This page covers the Eloquent-specific building blocks layered on top:
soft-delete helpers, auditing, optimistic locking, and the pessimistic `findByIdForUpdate()`.

## `EloquentRepository`

<!-- source: packages/data/tests/Fixtures/Repository/RecordRepository.php -->
```php
#[Repository]
class RecordRepository extends EloquentRepository
{
    protected string $model = Record::class;
    // …
}
```

Set `protected string $model = X::class;` and the repository inherits `save`/`saveAll`/`findById`/`findAll`/
`findAllById`/`existsById`/`count`/`delete`/`deleteById`/`deleteAll`, plus paging and sorting, all driven from
`($this->model)::query()`. `Page`/`Pageable`/`Sort` are mapped to the Eloquent builder at the edge —
`findPaged()` runs a `count()` query for the total, then `skip($pageable->offset())->take($pageable->size)`
for the slice; `applySort()` folds each `Sort` order into an `->orderBy($property, $direction)` call. Every
list-returning method narrows its raw Eloquent results back to `TModel` via an internal `instanceof
$this->model` check, so callers never see a bare `Model` — only the concrete entity type the repository was
declared over. This class is reflection-free (everything goes through the Eloquent `Builder`, no runtime
class introspection); see [Data & Repositories](data.md) for the derived-query and `#[Query]` dispatch it also
hosts.

Two reads insist where their siblings return null: `getById($id)` throws `EmptyResultDataAccessException`
(404) for a missing row, and `findByIdForUpdate($id)` reads under `SELECT … FOR UPDATE` and throws
`TransactionRequiredException` outside a transaction. `findSlice(Pageable)` is the count-free page.

Every method runs through the package's exception translation (see [Data & Repositories](data.md#exception-translation)):
a unique violation from `save()` is a `DuplicateKeyException`, a `#[Query]` against a missing table a
`BadSqlGrammarException`, a connection that cannot be opened a `DataAccessResourceFailureException` — each with
the `QueryException` as `previous`. `firefly.data.exception-translation.enabled=false` restores the raw
exceptions.

## Soft delete

`EloquentRepository` reuses Eloquent's native `SoftDeletes` trait as-is — nothing bespoke:

<!-- source: packages/data/tests/Fixtures/Repository/SoftRecord.php -->
```php
final class SoftRecord extends Model
{
    use SoftDeletes;

    protected $table = 'soft_records';

    public $timestamps = false;

    protected $guarded = [];
}
```

Once a model `use`s `SoftDeletes`, deleting through the repository (`delete()`/`deleteById()`) soft-deletes
the row, and every ordinary read (`findAll()`, `findById()`, derived queries, …) transparently excludes
trashed rows via Eloquent's own global scope — the repository adds no extra filtering of its own. Two helpers
round out the lifecycle:

<!-- source: packages/data/src/Repository/EloquentRepository.php -->
```php
public function findAllIncludingDeleted(): array
{
    $query = $this->reading(__FUNCTION__);

    return $this->translating(fn (): array => $this->narrow($query->withoutGlobalScope(SoftDeletingScope::class)->get()->all()));
}
// …
public function restore(mixed $id): ?object
{
    return $this->translating(function () use ($id): ?object {
        $this->query()
            ->withoutGlobalScope(SoftDeletingScope::class)
            ->where($this->keyName(), '=', $id)
            ->update(['deleted_at' => null]);

        return $this->findById($id);
    });
}
```

`findAllIncludingDeleted()` drops `SoftDeletingScope` for that query only (later calls still exclude trashed
rows as normal); `restore()` writes `deleted_at = null` for the given id and returns the freshly re-read
entity, or `null` if no such row exists. Both are meaningful only on a model that actually uses `SoftDeletes`
— on a plain model the scope is simply absent, so calling them is a harmless no-op.

## Auditing

`Auditable` is an opt-in trait for `created_by`/`updated_by` stamping:

<!-- source: packages/data/tests/Fixtures/Repository/AuditedRecord.php -->
```php
final class AuditedRecord extends Model
{
    use Auditable;

    protected $table = 'audited_records';

    public $timestamps = false;

    protected $guarded = [];
}
```

`use Auditable` registers an `AuditObserver` (deferred to Eloquent's `whenBooted()` hook, since
`Model::observe()` can't run while the model is still mid-boot) that stamps `created_by` and `updated_by` on
insert, and `updated_by` again on every update. Stamping is driven by an `AuditorAware` port:

<!-- source: packages/data/src/Repository/Auditing/AuditorAware.php -->
```php
interface AuditorAware
{
    public function currentAuditor(): int|string|null;
}
```

!!! note "Known-latent: auditing is a no-op before M11"
    `AuditObserver` resolves `AuditorAware` from the container at write time; while nothing is bound (which is
    the case until M11's security-context principal lands), it simply returns early and leaves `created_by`/
    `updated_by` untouched. Binding an `AuditorAware` implementation is what turns stamping on — no code change
    to `Auditable`-using models is needed once that lands.

## Optimistic locking

`HasOptimisticLock` guards concurrent writes to the same row with a `version` integer column:

<!-- source: packages/data/tests/Fixtures/Repository/VersionedRecord.php -->
```php
final class VersionedRecord extends Model
{
    use HasOptimisticLock;

    protected $table = 'versioned_records';

    public $timestamps = false;

    protected $guarded = [];

    protected $attributes = ['version' => 0];
}
```

The trait overrides Eloquent's internal `performUpdate()`: every update bumps `version` by one and adds
`WHERE version = <the version the row was loaded with>` to the `UPDATE`. If that `WHERE` matches zero rows —
because someone else's write already moved the version — the row changed underneath the caller, and
`OptimisticLockException` — a kernel `OptimisticLockingFailureException` (409 `OPTIMISTIC_LOCK`) — is thrown
instead of silently applying (or silently losing) the write. This is the package's own regression test for it,
which reads the row twice, writes through the first handle and then watches the second one be refused:

<!-- source: packages/data/tests/Repository/OptimisticLockTest.php -->
```php
$a = $repo->findById($id);
$b = $repo->findById($id);
assert($a instanceof VersionedRecord && $b instanceof VersionedRecord);

$a->name = 'first';
$repo->save($a);

$reloaded = $repo->findById($id);
expect($reloaded?->version)->toBe(1);

$b->name = 'second';
expect(fn () => $repo->save($b))->toThrow(OptimisticLockException::class);

expect($repo->findById($id)?->name)->toBe('first'); // the stale write never landed
```

Insert is untouched — a new row simply starts at its default `version`; only updates are guarded. The column
name defaults to `version` (`optimisticLockColumn()`, overridable per model).

## The domain-event bridge

`EloquentRepository::save()` does two things when it persists an entity: it calls `$entity->save()` if the
entity is an Eloquent `Model`, and — separately — if the entity also implements `RecordsDomainEvents` (either
by extending `AggregateRoot` or, for a `Model`, by `use HasDomainEvents`) **and** a transaction is currently
active on that entity's own connection, it registers the entity with the `AggregateTracker` unit-of-work
registry. That registration is what lets the framework drain and publish the entity's raised events after the
surrounding transaction commits — see [Domain (DDD)](domain.md#the-after-commit-event-model) for the event
model itself and [Transactions](transactional.md) for the commit/rollback machinery that drives the drain.
