# kitchen.ex: a stove with a few burners.
defmodule Kitchen.Stove do
  @moduledoc "A stove with a few burners."

  @burners 4

  defstruct heat: 0, lit: []

  @doc "Ignite a burner, up to the number the stove holds."
  def ignite(%__MODULE__{lit: lit} = stove, burner) when burner < @burners do
    %{stove | lit: [burner | lit]}
  end

  def ignite(stove, _burner), do: stove

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
