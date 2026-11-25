<?php
// Library.php is a synthetic fixture for the chunker tests.

namespace Town\Library;

const LOAN_DAYS = 21;

interface Lendable
{
    public function dueIn(int $days): int;
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

    public function dueIn(int $days): int
    {
        return $days + LOAN_DAYS;
    }

    public function lend(string $title): string
    {
        $this->books[] = $title;
        return $this->stamp($title);
    }
}

function overdue(int $days): bool
{
    return $days > LOAN_DAYS;
}
