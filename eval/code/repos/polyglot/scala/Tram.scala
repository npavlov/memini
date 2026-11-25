// Tram.scala: trams and the depot that parks them.
package town.trams

import scala.collection.mutable

val MaxRiders = 120

/** Anything that runs on rails and carries riders. */
trait Railcar {
  def seats: Int
}

enum Season {
  case Summer, Winter
}

case class Tram(name: String, cars: Int) extends Railcar {
  def seats: Int = cars * 40

  def admit(riders: Int): Boolean = riders <= seats
}

object Tram {
  def single(name: String): Tram = Tram(name, 1)
}

class Depot(val tracks: Int) {
  private val parked = mutable.Set.empty[String]

  def park(tram: Tram): Boolean = {
    if (parked.size >= tracks) false
    else {
      parked += tram.name
      true
    }
  }
}

def headway(seconds: Int): String = s"$seconds s"
