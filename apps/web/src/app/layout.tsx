import type { Metadata, Viewport } from "next";
import { Archivo, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "@/lib/auth-context";

const archivo = Archivo({
  subsets: ["latin"],
  variable: "--font-archivo",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
  display: "swap",
});

export const metadata: Metadata = {
  title: "FacturAI · Facturación Automática de Tickets CFDI 4.0",
  description: "Fotografía el ticket. La factura llega a tu correo.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  viewportFit: "cover",
  themeColor: "#7A1B3D",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="es" className={`${archivo.variable} ${jetbrainsMono.variable}`}>
      <body className="min-h-screen bg-bg text-ink selection:bg-brand-soft selection:text-brand">
        <AuthProvider>{children}</AuthProvider>
      </body>
    </html>
  );
}
