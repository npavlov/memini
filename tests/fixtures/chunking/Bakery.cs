// Bakery is a synthetic fixture for the chunker tests.
using System;
using System.Collections.Generic;

namespace Town.Bakery
{
    /// <summary>Anything that bakes.</summary>
    public interface IOven
    {
        int Heat(int minutes);
    }

    public enum Crust
    {
        Soft,
        Crisp,
    }

    public struct Loaf
    {
        public string Name;
        public Crust Crust;
    }

    public record Order(string Customer, int Loaves);

    public class Bakery : IOven
    {
        public const int OpensAt = 6;

        private readonly List<Loaf> shelf = new();

        public string Owner { get; set; } = "Ada Placeholder";

        public Bakery(string owner)
        {
            Owner = owner;
        }

        /// <summary>Degrees after the given minutes.</summary>
        public int Heat(int minutes)
        {
            return 180 + minutes;
        }

        public void Stock(Loaf loaf)
        {
            shelf.Add(loaf);
        }
    }
}
