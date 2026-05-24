# ADS – Beckhoff TwinCAT Integration für Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Version](https://img.shields.io/badge/version-0.1.1-blue.svg)](https://github.com/Errormaster007/ads-custom-component/releases)

Erweiterte ADS-Integration für Home Assistant zur Anbindung von Beckhoff TwinCAT-SPSen über das ADS-Protokoll.

**Neue Features (gegenüber offizieller Integration):**
- UI-Konfiguration über Config Flow
- GVL-Import (Service + Options-Dialog) für schnellen Variablenabgleich mit der Beckhoff SPS
- Ausführliches Logging mit Hub-Identifikation
- Update-Entity für HACS/Home Assistant mit Versionsprüfung gegen GitHub Releases

## Installation über HACS (empfohlen)

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Errormaster007&repository=ads-custom-component&category=integration)

Oder manuell als Custom Repository:
1. HACS → Integrationen → Drei Punkte → **Benutzerdefiniertes Repository**
2. URL: `https://github.com/Errormaster007/ads-custom-component`  
   Kategorie: Integration
3. **ADS – Beckhoff TwinCAT** installieren → Home Assistant neu starten
4. Einstellungen → Geräte & Dienste → **ADS** hinzufügen

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
