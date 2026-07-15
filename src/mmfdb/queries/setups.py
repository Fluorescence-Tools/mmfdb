"""Setup and calibration queries.

Instrument setups, detector channels, PIE windows, setup calibrations, and
setup-definition records — extracted from the MFDatabase god-class (PRD-26).
Methods run on the shared ``self.conn``/``self.dao`` and resolve cross-concern
calls via the MFDatabase MRO.
"""

from __future__ import annotations

from typing import Any

from mmfdb.schema._sqlutil import _json_dumps, _utc_now
from mmfdb.security.auth import create_default_acl_for_object


class SetupCalibMixin:
    def delete_setup(self, setup_id):
        # PRD-26 Task 2: schema-driven soft-delete (was a hand UPDATE). The explicit
        # _utc_now() value keeps the stored deleted_at marker format identical.
        with self.conn:
            self.dao.soft_delete("mmfdb_setup", setup_id, pk_column="setup_id", deleted_at=_utc_now())

    def list_detector_channels(self, setup_id: str) -> list[dict[str, Any]]:
        """List detector channel definitions for a setup.

        Parameters
        ----------
        setup_id : str
            Setup identifier.

        Returns
        -------
        list of dict
            Detector channel rows.
        """
        return self.dao.list("mmfdb_setup_detector_channel", filters={"setup_id": setup_id}, order_by="id")

    def list_pie_windows(self, setup_id: str) -> list[dict[str, Any]]:
        """List PIE/micro-time window definitions for a setup.

        Parameters
        ----------
        setup_id : str
            Setup identifier.

        Returns
        -------
        list of dict
            PIE window rows.
        """
        return self.dao.list("mmfdb_setup_pie_window", filters={"setup_id": setup_id}, order_by="id")

    def add_setup_calibration(
        self,
        setup_id: str,
        channel_name: str,
        g_factor: float | None = None,
        l1: float | None = None,
        l2: float | None = None,
        g_factor_channels: list[int] | None = None,
        g_factor_calibration_id: str | None = None,
        calibrated_at: str | None = None,
        method: str | None = "manual",
        created_by_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a calibration snapshot for one detector channel.

        This is an append-only insert — existing rows are never updated.
        The corresponding ``mmfdb_setup_detector_channel`` row is updated as
        a cache of the latest snapshot so that legacy readers continue to work.

        Parameters
        ----------
        setup_id : str
            Setup identifier.
        channel_name : str
            Detector channel name.
        g_factor : float or None, optional
            G-factor value.
        l1 : float or None, optional
            Leakage parameter l1.
        l2 : float or None, optional
            Leakage parameter l2.
        g_factor_channels : list of int or None, optional
            Channel indices used for G-factor calculation.
        g_factor_calibration_id : str or None, optional
            Reference to the MMFDB calibration artifact.
        calibrated_at : str or None, optional
            ISO-8601 timestamp. Defaults to current UTC time.
        method : str or None, optional
            Calibration method (e.g. ``manual``, ``migrated``,
            ``jordi_g_factor``). Defaults to ``manual``.
        created_by_user_id : str or None, optional
            User creating this snapshot.

        Returns
        -------
        dict
            The inserted row as a dictionary.
        """
        now = calibrated_at or _utc_now()
        with self._transaction():
            cur = self.conn.execute(
                """INSERT INTO mmfdb_setup_calibration
                    (setup_id, channel_name, g_factor, l1, l2,
                     g_factor_channels, g_factor_calibration_id,
                     calibrated_at, method, created_by_user_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    setup_id, channel_name, g_factor, l1, l2,
                    _json_dumps(g_factor_channels) if g_factor_channels is not None else None,
                    g_factor_calibration_id,
                    now, method, created_by_user_id,
                    now, now,
                ),
            )
            snapshot_id = cur.lastrowid

            # Update the detector channel cache row
            self.conn.execute(
                """UPDATE mmfdb_setup_detector_channel SET
                    g_factor = ?, l1 = ?, l2 = ?,
                    g_factor_channels = ?, g_factor_calibration_id = ?,
                    updated_at = ?
                WHERE setup_id = ? AND name = ? AND deleted_at IS NULL""",
                (
                    g_factor, l1, l2,
                    _json_dumps(g_factor_channels) if g_factor_channels is not None else None,
                    g_factor_calibration_id,
                    now, setup_id, channel_name,
                ),
            )

            return self.dao.get("mmfdb_setup_calibration", snapshot_id, include_deleted=True)

    def list_setup_calibration_dates(
        self, setup_id: str
    ) -> list[str]:
        """Return distinct calibration timestamps for a setup, newest first.

        Parameters
        ----------
        setup_id : str
            Setup identifier.

        Returns
        -------
        list of str
            ISO-8601 timestamps.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT calibrated_at FROM mmfdb_setup_calibration "
            "WHERE setup_id = ? ORDER BY calibrated_at DESC",
            (setup_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_setup_calibration(
        self,
        setup_id: str,
        calibrated_at: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return calibration snapshots for a setup at a given timestamp.

        Parameters
        ----------
        setup_id : str
            Setup identifier.
        calibrated_at : str or None, optional
            ISO-8601 timestamp. If ``None``, returns the latest snapshot
            per channel.

        Returns
        -------
        list of dict
            Calibration snapshot rows.
        """
        if calibrated_at:
            # raw read filter has no deleted_at guard — preserve via include_deleted
            rows = self.dao.list(
                "mmfdb_setup_calibration",
                filters={"setup_id": setup_id, "calibrated_at": calibrated_at},
                include_deleted=True, order_by="channel_name",
            )
            return rows
        else:
            rows = self.conn.execute(
                """SELECT sc.* FROM mmfdb_setup_calibration sc
                    INNER JOIN (
                        SELECT channel_name, MAX(calibrated_at) AS latest
                        FROM mmfdb_setup_calibration
                        WHERE setup_id = ?
                        GROUP BY channel_name
                    ) latest
                    ON sc.channel_name = latest.channel_name
                    AND sc.calibrated_at = latest.latest
                    WHERE sc.setup_id = ?
                    ORDER BY sc.channel_name""",
                (setup_id, setup_id),
            ).fetchall()
        return [dict(r) for r in rows]

    def save_setup(
        self,
        setup_id: str,
        name: str,
        version: int = 1,
        instrument_id: str | None = None,
        description: str | None = None,
        configuration: dict[str, Any] | None = None,
        detectors: dict[str, Any] | None = None,
        windows: dict[str, Any] | None = None,
        timing_calibration: dict[str, Any] | None = None,
        irf_definition: dict[str, Any] | None = None,
        dark_count: dict[str, Any] | None = None,
        timing_resolution: dict[str, Any] | None = None,
        burst_defaults: dict[str, Any] | None = None,
        fcs_calibration: dict[str, Any] | None = None,
        created_by_user_id: str | None = None,
        is_public: bool | int | None = None,
        fcs_pairs: dict[str, Any] | None = None,
        n_bins: int | None = None,
        n_casc: int | None = None,
        make_fine: bool | int | None = None,
    ) -> None:
        now = _utc_now()
        # Extract typed timing columns from the timing_resolution dict.
        # These are the dictionary-authoritative columns; timing_resolution_json
        # remains for backward compatibility.
        timing_dict = timing_resolution or {}
        _macro_t = timing_dict.get("macro_time_resolution")
        _micro_t = timing_dict.get("micro_time_resolution")
        _micro_b = timing_dict.get("micro_time_binning")
        # Normalize empty-string user_id to None so the FK constraint holds
        _owner = created_by_user_id or None
        with self._transaction():
            existing = self.conn.execute(
                "SELECT created_by_user_id, is_public FROM mmfdb_setup WHERE setup_id = ?",
                (setup_id,),
            ).fetchone()
            effective_owner = (existing[0] if existing else None) or _owner
            effective_public = (
                int(bool(is_public))
                if is_public is not None
                else int(bool(existing[1])) if existing else 0
            )
            self.conn.execute(
                """INSERT INTO mmfdb_setup (
                    setup_id, name, version, instrument_id, description,
                    configuration_json, detectors_json, timing_calibration_json,
                    irf_definition_json, dark_count_json, timing_resolution_json,
                    macro_time_resolution, micro_time_resolution, micro_time_binning,
                    n_bins, n_casc, make_fine,
                    burst_defaults_json, fcs_calibration_json,
                    created_by_user_id,
                    is_public,
                    created_at, updated_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(setup_id) DO UPDATE SET
                    name=excluded.name,
                    version=excluded.version,
                    instrument_id=excluded.instrument_id,
                    description=excluded.description,
                    configuration_json=excluded.configuration_json,
                    detectors_json=excluded.detectors_json,
                    timing_calibration_json=excluded.timing_calibration_json,
                    irf_definition_json=excluded.irf_definition_json,
                    dark_count_json=excluded.dark_count_json,
                    timing_resolution_json=excluded.timing_resolution_json,
                    macro_time_resolution=excluded.macro_time_resolution,
                    micro_time_resolution=excluded.micro_time_resolution,
                    micro_time_binning=excluded.micro_time_binning,
                    n_bins=excluded.n_bins,
                    n_casc=excluded.n_casc,
                    make_fine=excluded.make_fine,
                    burst_defaults_json=excluded.burst_defaults_json,
                    fcs_calibration_json=excluded.fcs_calibration_json,
                    created_by_user_id=COALESCE(mmfdb_setup.created_by_user_id, excluded.created_by_user_id),
                    is_public=excluded.is_public,
                    updated_at=excluded.updated_at,
                    deleted_at=excluded.deleted_at""",
                (
                    setup_id,
                    name,
                    version,
                    instrument_id,
                    description,
                    _json_dumps(configuration),
                    _json_dumps(detectors),
                    _json_dumps(timing_calibration),
                    _json_dumps(irf_definition),
                    _json_dumps(dark_count),
                    _json_dumps(timing_resolution),
                    _macro_t,
                    _micro_t,
                    _micro_b,
                    n_bins,
                    n_casc,
                    1 if make_fine else 0 if make_fine is not None else None,
                    _json_dumps(burst_defaults),
                    _json_dumps(fcs_calibration),
                    effective_owner,
                    effective_public,
                    now,
                    now,
                    None,
                ),
            )
            if effective_owner:
                create_default_acl_for_object(
                    self.conn,
                    "setup",
                    setup_id,
                    owner_user_id=effective_owner,
                    mode=0o704 if effective_public else 0o700,
                )
                if existing and is_public is not None:
                    self.conn.execute(
                        "UPDATE mmfdb_object_acl SET mode = ?, updated_at = ? "
                        "WHERE object_type = 'setup' AND object_id = ? AND deleted_at IS NULL",
                        (0o704 if effective_public else 0o700, now, setup_id),
                    )

            # Write structured detector channel rows
            if detectors:
                self.conn.execute(
                    "UPDATE mmfdb_setup_detector_channel SET deleted_at = ? WHERE setup_id = ? AND deleted_at IS NULL",
                    (now, setup_id)
                )
                for det_name, det_data in detectors.items():
                    if not isinstance(det_data, dict):
                        continue
                    channels = det_data.get("channels") or det_data.get("chs")
                    mtr = det_data.get("micro_time_ranges")
                    g_factor = det_data.get("g_factor")
                    l1 = det_data.get("l1")
                    l2 = det_data.get("l2")
                    gfc = det_data.get("g_factor_channels")
                    gfc_id = det_data.get("g_factor_calibration_id")
                    self.conn.execute(
                        """INSERT INTO mmfdb_setup_detector_channel
                            (setup_id, name, channels, micro_time_ranges,
                             g_factor, l1, l2, g_factor_channels, g_factor_calibration_id,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            setup_id, det_name,
                            _json_dumps(channels) if channels is not None else None,
                            _json_dumps(mtr) if mtr is not None else None,
                            g_factor, l1, l2,
                            _json_dumps(gfc) if gfc is not None else None,
                            gfc_id,
                            now, now
                        )
                    )
                    # Append a calibration snapshot only when calibration data is
                    # present AND differs from the latest snapshot for this
                    # channel. Plain structural re-saves must not create
                    # duplicate-factor snapshots (which would pollute the
                    # calibration-date history).
                    has_cal = g_factor is not None or l1 is not None or l2 is not None
                    latest_rows = self.dao.list(
                        "mmfdb_setup_calibration",
                        filters={"setup_id": setup_id, "channel_name": det_name},
                        order_by="calibrated_at", descending=True, limit=1,
                    )
                    latest = latest_rows[0] if latest_rows else None
                    unchanged = latest is not None and (
                        latest["g_factor"] == g_factor and latest["l1"] == l1
                        and latest["l2"] == l2 and latest["g_factor_calibration_id"] == gfc_id
                    )
                    if has_cal and not unchanged:
                        self.conn.execute(
                            """INSERT INTO mmfdb_setup_calibration
                                (setup_id, channel_name, g_factor, l1, l2,
                                 g_factor_channels, g_factor_calibration_id,
                                 calibrated_at, method, created_by_user_id,
                                 created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                setup_id, det_name, g_factor, l1, l2,
                                _json_dumps(gfc) if gfc is not None else None,
                                gfc_id,
                                now, "manual", _owner,
                                now, now,
                            ),
                        )

            # Write structured PIE window rows
            if windows:
                self.conn.execute(
                    "UPDATE mmfdb_setup_pie_window SET deleted_at = ? WHERE setup_id = ? AND deleted_at IS NULL",
                    (now, setup_id)
                )
                for win_name, bounds in windows.items():
                    if not isinstance(bounds, (list, tuple)) or len(bounds) < 2:
                        continue
                    start_val, end_val = int(bounds[0]), int(bounds[1])
                    self.conn.execute(
                        """INSERT INTO mmfdb_setup_pie_window
                            (setup_id, name, start, end, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (setup_id, win_name, start_val, end_val, now, now)
                    )

            # Write structured FCS channel pair rows
            if fcs_pairs:
                self.conn.execute(
                    "UPDATE mmfdb_setup_fcs_pair SET deleted_at = ? WHERE setup_id = ? AND deleted_at IS NULL",
                    (now, setup_id)
                )
                for pair_name, pair_data in fcs_pairs.items():
                    if isinstance(pair_data, dict):
                        channel_a = pair_data.get("channel_a", "")
                        channel_b = pair_data.get("channel_b", "")
                        kind = pair_data.get("kind")
                        pair_n_bins = pair_data.get("n_bins")
                        pair_n_casc = pair_data.get("n_casc")
                        pair_make_fine = pair_data.get("make_fine")
                    elif isinstance(pair_data, (list, tuple)) and len(pair_data) >= 2:
                        channel_a = str(pair_data[0]) if pair_data[0] else ""
                        channel_b = str(pair_data[1]) if pair_data[1] else ""
                        kind = str(pair_data[2]) if len(pair_data) > 2 and pair_data[2] else None
                        pair_n_bins = pair_n_casc = pair_make_fine = None
                    else:
                        continue
                    if not channel_a or not channel_b:
                        continue
                    self.conn.execute(
                        """INSERT INTO mmfdb_setup_fcs_pair
                            (setup_id, name, channel_a, channel_b, kind,
                             n_bins, n_casc, make_fine,
                             created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            setup_id, pair_name, channel_a, channel_b, kind,
                            pair_n_bins, pair_n_casc,
                            1 if pair_make_fine else 0 if pair_make_fine is not None else None,
                            now, now,
                        )
                    )

            self.add_audit_log(
                action="create",
                target_type="setup",
                target_id=setup_id,
                details={"name": name, "version": version},
            )

    def get_setup(
        self,
        setup_id: str,
        calibrated_at: str | None = None,
    ) -> dict[str, Any] | None:
        result = self.dao.get("mmfdb_setup", setup_id, include_deleted=True)
        if result is not None:
            result["detector_channels"] = self.list_detector_channels(setup_id)
            result["pie_windows"] = self.list_pie_windows(setup_id)
            result["fcs_pairs"] = self.dao.list(
                "mmfdb_setup_fcs_pair", filters={"setup_id": setup_id}, order_by="id"
            )
            result["calibration_dates"] = self.list_setup_calibration_dates(setup_id)
            result["calibration"] = self.get_setup_calibration(setup_id, calibrated_at=calibrated_at)
        return result

    def list_setups(self) -> list[dict[str, Any]]:
        """List setup snapshots.

        Returns
        -------
        list of dict
            Setup dictionaries.
        """
        rows = self.conn.execute("SELECT * FROM mmfdb_setup WHERE deleted_at IS NULL ORDER BY name, version").fetchall()
        return [dict(r) for r in rows]
