// Kite.kt is a synthetic fixture for the chunker tests.
package town.kites

import kotlin.math.max

const val MAX_STRING_METRES = 60

/** Anything that flies on a string. */
interface Flyer {
    fun lift(wind: Int): Int
}

enum class Gust { CALM, FRESH, STRONG }

data class Kite(val colour: String, val tails: Int) : Flyer {
    override fun lift(wind: Int): Int {
        return max(0, wind - tails)
    }

    fun shorten(): Kite = copy(tails = tails - 1)

    companion object {
        fun plain(): Kite = Kite("white", 0)
    }
}

object Wind {
    var speed = 3

    fun gust(): Gust = if (speed > 10) Gust.STRONG else Gust.FRESH
}

fun Kite.launch(wind: Int): Boolean = lift(wind) > 2

fun sky(kites: List<Kite>): Int {
    var total = 0
    for (kite in kites) {
        total += kite.tails
    }
    return total
}
