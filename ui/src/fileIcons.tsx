/**
 * File-type icons (DESIGN.md §6.2): 16px, single-color stroke glyphs in
 * --text-tertiary. Hand-inlined paths — deliberately no icon dependency.
 */

import type { ReactNode } from "react";

export type FileIconKind =
  | "code"
  | "py"
  | "ts"
  | "js"
  | "json"
  | "md"
  | "doc"
  | "sheet"
  | "slides"
  | "css"
  | "html"
  | "config"
  | "generic";

const EXT_KINDS: Record<string, FileIconKind> = {
  py: "py",
  pyi: "py",
  ts: "ts",
  tsx: "ts",
  mts: "ts",
  cts: "ts",
  js: "js",
  jsx: "js",
  mjs: "js",
  cjs: "js",
  json: "json",
  jsonc: "json",
  md: "md",
  markdown: "md",
  docx: "doc",
  doc: "doc",
  odt: "doc",
  rtf: "doc",
  txt: "doc",
  xlsx: "sheet",
  xlsm: "sheet",
  xls: "sheet",
  csv: "sheet",
  ods: "sheet",
  pptx: "slides",
  ppt: "slides",
  odp: "slides",
  css: "css",
  scss: "css",
  less: "css",
  html: "html",
  htm: "html",
  yml: "config",
  yaml: "config",
  toml: "config",
  ini: "config",
  cfg: "config",
  conf: "config",
  env: "config",
  lock: "config",
  c: "code",
  h: "code",
  cpp: "code",
  hpp: "code",
  cs: "code",
  rs: "code",
  go: "code",
  java: "code",
  rb: "code",
  php: "code",
  sh: "code",
  bash: "code",
  ps1: "code",
  psm1: "code",
  sql: "code",
  r: "code",
  jl: "code",
  ipynb: "code",
};

export function fileIconKind(name: string): FileIconKind {
  const lower = name.toLowerCase();
  // Dotfiles (.gitignore, .env, …) are configuration by convention.
  if (lower.startsWith(".") && !lower.slice(1).includes(".")) return "config";
  const dot = lower.lastIndexOf(".");
  if (dot < 0) return "generic";
  return EXT_KINDS[lower.slice(dot + 1)] ?? "generic";
}

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="wb-file-icon"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const FILE_OUTLINE = (
  <>
    <path d="M4 1.5h5L12.5 5v9a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5Z" />
    <path d="M9 1.5V5h3.5" />
  </>
);

const S_CURVE = (
  <path d="M13.2 6.3c-.5-.5-1.2-.8-2-.8-1.1 0-2 .6-2 1.5 0 2.2 4.2 1.1 4.2 3.3 0 .9-.9 1.5-2.1 1.5-.9 0-1.7-.3-2.2-.9" />
);

const GLYPHS: Record<FileIconKind, ReactNode> = {
  generic: FILE_OUTLINE,
  doc: (
    <>
      {FILE_OUTLINE}
      <path d="M5.5 8h5M5.5 10h5M5.5 12h3" />
    </>
  ),
  sheet: (
    <>
      <rect x="2.5" y="3" width="11" height="10" rx="1" />
      <path d="M2.5 6h11M2.5 9.5h11M6.5 3v10" />
    </>
  ),
  slides: (
    <>
      <rect x="2.5" y="2.5" width="11" height="8" rx="1" />
      <path d="M8 10.5v2M5.5 14.5l2.5-2 2.5 2" />
    </>
  ),
  code: <path d="M5 4.5 2.5 8 5 11.5M11 4.5 13.5 8 11 11.5M9.5 3l-3 10" />,
  py: (
    <>
      <path d="M10.8 4.6c0-1.3-1.2-2.1-2.8-2.1S5.2 3.3 5.2 4.6 6.4 6.7 8 6.7s2.8.8 2.8 2.1v2.6c0 1.3-1.2 2.1-2.8 2.1s-2.8-.8-2.8-2.1" />
      <circle cx="6.7" cy="4.4" r="0.6" fill="currentColor" stroke="none" />
      <circle cx="9.3" cy="11.6" r="0.6" fill="currentColor" stroke="none" />
    </>
  ),
  ts: (
    <>
      <path d="M2.5 5.5h5M5 5.5V12" />
      {S_CURVE}
    </>
  ),
  js: (
    <>
      <path d="M6 4.5v5.6c0 1.2-.8 1.9-2 1.9-.8 0-1.4-.3-1.8-.9" />
      {S_CURVE}
    </>
  ),
  json: (
    <>
      <path d="M6 2.5c-1.2 0-1.8.6-1.8 1.7v1.9c0 .8-.4 1.3-1.2 1.6v.6c.8.3 1.2.8 1.2 1.6v1.9c0 1.1.6 1.7 1.8 1.7" />
      <path d="M10 2.5c1.2 0 1.8.6 1.8 1.7v1.9c0 .8.4 1.3 1.2 1.6v.6c-.8.3-1.2.8-1.2 1.6v1.9c0 1.1-.6 1.7-1.8 1.7" />
    </>
  ),
  md: (
    <>
      <path d="M2.5 10.5v-5l2.25 2.5L7 5.5v5" />
      <path d="M11.5 5.5v5M9.5 8.5l2 2 2-2" />
    </>
  ),
  css: <path d="M6.5 3.5l-1 9M10.5 3.5l-1 9M3.5 6.5h9M3 9.5h9" />,
  html: <path d="M5.5 4.5 2.5 8l3 3.5M10.5 4.5 13.5 8l-3 3.5" />,
  config: (
    <>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 2.2v2M8 11.8v2M2.2 8h2M11.8 8h2M3.9 3.9l1.4 1.4M10.7 10.7l1.4 1.4M12.1 3.9l-1.4 1.4M5.3 10.7l-1.4 1.4" />
    </>
  ),
};

export function FileTypeIcon({ name }: { name: string }) {
  return <Icon>{GLYPHS[fileIconKind(name)]}</Icon>;
}
