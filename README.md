# ADS Custom Component (Test Build)

Dieses Paket enthaelt die angepasste ADS-Integration als `custom_components`-Version zum schnellen Testen.

## Installation ueber HACS (Custom Repository)

1. Lege dieses Verzeichnis als eigenes Git-Repository an und pushe es nach GitHub (oder GitLab).
2. In Home Assistant -> HACS -> Integrationen -> Drei Punkte -> Benutzerdefinierte Repositories:
   - Repository-URL: URL deines Repos
   - Kategorie: Integration
3. Danach in HACS nach `ADS (Custom Test Build)` suchen und installieren.
4. Home Assistant neu starten.
5. Unter Einstellungen -> Geraete & Dienste `ADS` hinzufuegen.

## Installation (manuell)

1. Home Assistant stoppen.
2. Ordner `custom_components/ads` aus diesem Paket nach deinem HA-Config-Verzeichnis kopieren:
   - Ziel: `<DEIN_CONFIG_PFAD>/custom_components/ads`
3. Home Assistant starten.
4. Unter Einstellungen -> Geraete & Dienste die Integration `ADS` hinzufuegen.

## Installation (PowerShell)

Nutze das Script `install_to_ha_config.ps1` in diesem Ordner.

Beispiel:

```powershell
.\install_to_ha_config.ps1 -HaConfigPath "D:\homeassistant\config"
```

Danach Home Assistant neu starten.

## Hinweis

- Diese Variante ist nur fuer Tests gedacht.
- Bei Core-Updates kann eine neue Kopie noetig sein.
