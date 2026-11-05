import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  output: "standalone", // a self-contained server for the container image
  cacheComponents: true,
  partialPrefetching: true,
};

export default nextConfig;
