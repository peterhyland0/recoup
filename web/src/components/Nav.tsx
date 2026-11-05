"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/", label: "Overview", match: ["/"] },
  { href: "/findings", label: "Findings", match: ["/findings", "/finding"] },
  { href: "/review", label: "Review queue", match: ["/review"] },
  { href: "/appeals", label: "Appeals", match: ["/appeals", "/appeal"] },
  { href: "/contracts", label: "Contracts", match: ["/contracts", "/contract"] },
  { href: "/evals", label: "Evals", match: ["/evals"] },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav aria-label="Main">
      {NAV.map((item) => {
        const current = item.match.includes(path);
        return (
          <Link key={item.href} href={item.href} aria-current={current ? "page" : undefined} className={current ? "current" : ""}>
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
