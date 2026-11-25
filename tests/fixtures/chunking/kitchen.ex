# kitchen.ex is a synthetic fixture for the chunker tests.
defmodule Kitchen.Stove do
  @moduledoc "A stove with a few burners."

  @burners 4

  defstruct heat: 0, lit: []

  @doc "Light a burner, up to the number the stove has."
  def light(%__MODULE__{lit: lit} = stove, burner) when burner < @burners do
    %{stove | lit: [burner | lit]}
  end

  def light(stove, _burner), do: stove

  defp warm(stove), do: %{stove | heat: stove.heat + 10}

  defmacro simmer(do: block) do
    quote do
      unquote(block)
    end
  end
end

defmodule Kitchen.Pan do
  def size(:small), do: 20
  def size(:large), do: 32
end
