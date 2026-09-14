import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: "var(--bg)",
        panel: {
          DEFAULT: "var(--panel)",
          2: "var(--panel-2)",
          3: "var(--panel-3)",
        },
        ink: {
          DEFAULT: "var(--ink)",
          2: "var(--ink-2)",
        },
        muted: "var(--muted)",
        line: {
          DEFAULT: "var(--line)",
          2: "var(--line-2)",
        },
        brand: {
          DEFAULT: "var(--brand)",
          soft: "var(--brand-soft)",
          on: "var(--on-brand)",
        },
        ok: {
          DEFAULT: "var(--ok)",
          soft: "var(--ok-soft)",
        },
        warn: {
          DEFAULT: "var(--warn)",
          soft: "var(--warn-soft)",
        },
        live: {
          DEFAULT: "var(--live)",
          soft: "var(--live-soft)",
        },
        bad: {
          DEFAULT: "var(--bad)",
          soft: "var(--bad-soft)",
        },
      },
      fontFamily: {
        sans: ["var(--font-archivo)", "system-ui", "-apple-system", "sans-serif"],
        mono: ["var(--font-mono)", "JetBrains Mono", "ui-monospace", "monospace"],
      },
      boxShadow: {
        prototipo: "var(--sh)",
      },
    },
  },
  plugins: [],
};

export default config;
