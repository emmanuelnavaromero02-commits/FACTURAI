import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  allowedDevOrigins: [
    "192.168.100.21",
    "192.168.100.21:3000",
    "localhost:3000",
    "*.trycloudflare.com",
  ],
  // Permitir carga de imágenes externas si es necesario
  images: {
    remotePatterns: [
      {
        protocol: "http",
        hostname: "**",
      },
      {
        protocol: "https",
        hostname: "**",
      },
    ],
  },
  async redirects() {
    return [
      {
        source: "/",
        destination: "/app",
        permanent: false,
      },
    ];
  },
  async rewrites() {
    return [
      {
        source: "/v1/:path*",
        destination: "http://127.0.0.1:8000/v1/:path*",
      },
    ];
  },
};

export default nextConfig;
