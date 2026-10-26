// Decorative hero illustration: a mini balance-game round that rotates through examples.
import { useCycle } from "./hooks";

interface DemoSide {
  emoji: string;
  label: string;
  color: string;
}

const DEMOS: ReadonlyArray<{ prompt: string; a: DemoSide; b: DemoSide }> = [
  {
    prompt: "Mint chocolate is…",
    a: { emoji: "🍫", label: "A dessert", color: "#12a150" },
    b: { emoji: "🪥", label: "Toothpaste", color: "#2f5bff" },
  },
  {
    prompt: "Pineapple on pizza?",
    a: { emoji: "🍍", label: "Tropical genius", color: "#ea580c" },
    b: { emoji: "🚨", label: "Call the police", color: "#141414" },
  },
  {
    prompt: "Pick one. Forever.",
    a: { emoji: "🥵", label: "Puffer jacket in July", color: "#ff3b30" },
    b: { emoji: "🥶", label: "T-shirt in January", color: "#0369a1" },
  },
  {
    prompt: "Group chat energy:",
    a: { emoji: "🔕", label: "Muted since 2019", color: "#c026d3" },
    b: { emoji: "⚡", label: "Replies in 0.2 sec", color: "#b45309" },
  },
];

export function DemoDuel() {
  const index = useCycle(DEMOS.length, 3400);
  const demo = DEMOS[index];
  const pickedSide = index % 2; // alternate which side "wins" so neither looks like the right answer
  return (
    <div className="j-demo" aria-hidden="true">
      <div key={index} className="j-demo__round">
        <p className="j-demo__prompt">{demo.prompt}</p>
        <div className="j-demo__duel">
          {[demo.a, demo.b].map((side, i) => (
            <div
              key={side.label}
              className={`j-demo__card${i === pickedSide ? " is-picked" : " is-other"}`}
              style={{ background: side.color }}
            >
              <span className="j-demo__emoji">{side.emoji}</span>
              <span className="j-demo__label">{side.label}</span>
            </div>
          ))}
          <span className="j-demo__vs">VS</span>
        </div>
      </div>
      <span className="j-demo__sticker">10 rounds</span>
    </div>
  );
}
