// Ferry.scala is a synthetic fixture for the chunker tests.
package town.ferries

import scala.collection.mutable

val MaxPassengers = 120

/** Anything that floats and carries people. */
trait Vessel {
  def capacity: Int
}

enum Weather {
  case Calm, Rough
}

case class Ferry(name: String, decks: Int) extends Vessel {
  def capacity: Int = decks * 40

  def board(people: Int): Boolean = people <= capacity
}

object Ferry {
  def small(name: String): Ferry = Ferry(name, 1)
}

class Pier(val berths: Int) {
  private val moored = mutable.Set.empty[String]

  def dock(ferry: Ferry): Boolean = {
    if (moored.size >= berths) false
    else {
      moored += ferry.name
      true
    }
  }
}

def crossing(minutes: Int): String = s"$minutes min"
