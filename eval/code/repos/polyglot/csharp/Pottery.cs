// Pottery: the kiln of a pottery studio.
using System;
using System.Collections.Generic;

namespace Studio.Pottery
{
    /// <summary>Anything that fires clay.</summary>
    public interface IKiln
    {
        int Fire(int cycles);
    }

    public enum Glaze
    {
        Matte,
        Gloss,
    }

    public struct Vase
    {
        public string Shape;
        public Glaze Glaze;
    }

    public record Commission(string Patron, int Vases);

    public class Pottery : IKiln
    {
        public const int OpensAt = 6;

        private readonly List<Vase> shelf = new();

        public string Potter { get; init; } = "Ada Placeholder";

        public Pottery(string potter)
        {
            Potter = potter;
        }

        /// <summary>Kiln heat after the given cycles.</summary>
        public int Fire(int cycles)
        {
            return 900 + cycles * 40;
        }

        public void Stock(Vase vase)
        {
            shelf.Add(vase);
        }
    }
}
