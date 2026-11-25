<?php
// Library.php: a small lending library.

namespace Town\Library;

const LOAN_WEEKS = 3;

interface Lendable
{
    public function dueIn(int $weeks): int;
}

trait Stamps
{
    public function stamp(string $title): string
    {
        return "{$title} (stamped)";
    }
}

enum Shelf: string
{
    case Fiction = 'F';
    case Atlas = 'A';
}

/**
 * A small lending library.
 */
class Library implements Lendable
{
    use Stamps;

    public const OPENS_AT = 9;

    private array $books = [];

    public function __construct(private string $name)
    {
    }

    public function dueIn(int $weeks): int
    {
        return $weeks + LOAN_WEEKS;
    }

    public function lend(string $title): string
    {
        $this->books[] = $title;
        return $this->stamp($title);
    }
}

function overdue(int $weeks): bool
{
    return $weeks > LOAN_WEEKS;
}
