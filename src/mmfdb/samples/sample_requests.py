"""Request data classes for sample operations.

This module provides canonical input objects for sample-related operations,
following the pattern established by other plugins (burst_selection, fret, etc.).
These Request classes are used as input to API endpoints, RPC handlers, and
GUI dialogs, ensuring type safety and consistent validation.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from mmfdb.models import (
    COMMON_PROBE_NAMES,
    ENTITY_TYPES,
    EntityDefinition,
    FretPairDefinition,
    ProbeDefinition,
    SampleDefinition,
    validate_vocabulary,
)


@dataclass
class SampleCreateRequest:
    """Request to create a new sample.

    This is the canonical input for sample creation through API, RPC, and GUI.
    All fields are optional except ``name``.

    Attributes
    ----------
    name : str
        Human-readable sample name (required).
    description : str, optional
        Free-text sample description.
    entities : list of EntityDefinition, optional
        List of biomolecular entities in the sample (PRD-02).
    probes : list of ProbeDefinition, optional
        List of all probes on the sample (PRD-02).
    fret_pairs : list of FretPairDefinition, optional
        List of FRET pairs defining energy transfer relationships (PRD-02).
    buffer_description : str, optional
        Buffer description, for example ``"PBS pH 7.4"``.
    ph : float, optional
        Sample pH.
    temperature_k : float, optional
        Temperature in Kelvin.
    salt_concentration_m : float, optional
        Salt concentration in mol/L.
    extra : dict, optional
        Additional metadata stored as JSON.
    validate_vocabulary : bool, optional
        If ``True`` (default), validate entity types and probe names against
        known vocabularies.

    """

    name: str
    description: str = ""
    entities: list[EntityDefinition] = field(default_factory=list)
    probes: list[ProbeDefinition] = field(default_factory=list)
    fret_pairs: list[FretPairDefinition] = field(default_factory=list)
    buffer_description: str = ""
    ph: Optional[float] = None
    temperature_k: Optional[float] = None
    salt_concentration_m: Optional[float] = None
    extra: dict = field(default_factory=dict)
    validate_vocabulary: bool = True  # R12-6: renamed for consistency with SampleDefinition

    def __post_init__(self) -> None:
        """Validate vocabulary fields if validation is enabled."""
        if not self.name or not self.name.strip():
            raise ValueError("sample name is required")

        if self.validate_vocabulary:
            self._validate_vocabulary()

    def _validate_vocabulary(self) -> None:
        """Validate entity types and probe names against known vocabularies.

        Unknown entity types hard-reject; unknown probe names only warn, since
        custom dyes are valid (PRD-02).
        """
        for i, entity in enumerate(self.entities):
            if entity.entity_type:
                validate_vocabulary(entity.entity_type, ENTITY_TYPES, "entity_type")

        logger = logging.getLogger(__name__)
        for i, probe in enumerate(self.probes):
            if probe.name and probe.name not in COMMON_PROBE_NAMES:
                logger.warning(
                    f"probes[{i}].name '{probe.name}' not in COMMON_PROBE_NAMES. "
                    f"Consider using a standard probe name."
                )

    def to_sample_definition(self) -> SampleDefinition:
        """Convert this request to a ``SampleDefinition``.

        Returns
        -------
        SampleDefinition
            Equivalent sample definition for use with sample_manager functions.
        """
        return SampleDefinition(
            name=self.name,
            description=self.description,
            entities=self.entities,
            probes=self.probes,
            fret_pairs=self.fret_pairs,
            buffer_description=self.buffer_description,
            ph=self.ph,
            temperature_k=self.temperature_k,
            salt_concentration_m=self.salt_concentration_m,
            extra=self.extra,
            validate_vocabulary=self.validate_vocabulary,
        )


@dataclass
class SampleUpdateRequest:
    """Request to update an existing sample.

    Attributes
    ----------
    sample_id : str
        The sample ID to update (required).
    name : str, optional
        New display name. If not provided, the existing name is kept.
    description : str, optional
        New description. If not provided, the existing description is kept.
    entities : list of EntityDefinition, optional
        New list of biomolecular entities (PRD-02).
    probes : list of ProbeDefinition, optional
        New list of all probes on the sample (PRD-02).
    fret_pairs : list of FretPairDefinition, optional
        New list of FRET pairs (PRD-02).
    buffer_description : str, optional
        New buffer description.
    ph : float, optional
        New pH value.
    temperature_k : float, optional
        New temperature in Kelvin.
    salt_concentration_m : float, optional
        New salt concentration in mol/L.
    extra : dict, optional
        Additional metadata to merge into existing extra.
    validate_vocabulary : bool, optional
        If ``True`` (default), validate entity types and probe names.

    """

    sample_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    entities: Optional[list[EntityDefinition]] = None
    probes: Optional[list[ProbeDefinition]] = None
    fret_pairs: Optional[list[FretPairDefinition]] = None
    buffer_description: Optional[str] = None
    ph: Optional[float] = None
    temperature_k: Optional[float] = None
    salt_concentration_m: Optional[float] = None
    extra: dict = field(default_factory=dict)
    validate_vocabulary: bool = True

    def __post_init__(self) -> None:
        """Validate vocabulary fields if validation is enabled."""
        if not self.sample_id:
            raise ValueError("sample_id is required for update")

        if self.validate_vocabulary:
            self._validate_vocabulary()

    def _validate_vocabulary(self) -> None:
        """Validate entity types and probe names against known vocabularies.

        Unknown entity types hard-reject; unknown probe names only warn.
        """
        for i, entity in enumerate(self.entities or []):
            if entity.entity_type:
                validate_vocabulary(entity.entity_type, ENTITY_TYPES, "entity_type")

        logger = logging.getLogger(__name__)
        for i, probe in enumerate(self.probes or []):
            if probe.name and probe.name not in COMMON_PROBE_NAMES:
                logger.warning(
                    f"probes[{i}].name '{probe.name}' not in COMMON_PROBE_NAMES. "
                    f"Consider using a standard probe name."
                )


@dataclass
class SampleQueryRequest:
    """Request to query samples with filters.

    Attributes
    ----------
    name_contains : str, optional
        Filter by display name containing this substring (case-insensitive).
    entity_type : str, optional
        Filter by entity type.
    probe_name : str, optional
        Filter by probe name (donor or acceptor).
    ph_min : float, optional
        Minimum pH value for filtering.
    ph_max : float, optional
        Maximum pH value for filtering.
    temperature_min : float, optional
        Minimum temperature in Kelvin for filtering.
    temperature_max : float, optional
        Maximum temperature in Kelvin for filtering.
    limit : int, optional
        Maximum number of results to return. Default is 100.
    offset : int, optional
        Number of results to skip. Default is 0.

    """

    name_contains: Optional[str] = None
    entity_type: Optional[str] = None
    probe_name: Optional[str] = None
    ph_min: Optional[float] = None
    ph_max: Optional[float] = None
    temperature_min: Optional[float] = None
    temperature_max: Optional[float] = None
    limit: int = 100
    offset: int = 0

    def __post_init__(self) -> None:
        """Validate vocabulary fields."""
        if self.entity_type:
            validate_vocabulary(
                self.entity_type,
                ENTITY_TYPES,
                "entity_type"
            )

        # Per PRD-02: unknown probe names should WARN (not reject), since custom
        # dyes are valid. Only entity_type should hard-reject.
        logger = logging.getLogger(__name__)
        if self.probe_name:
            if self.probe_name not in COMMON_PROBE_NAMES:
                logger.warning(
                    f"probe_name '{self.probe_name}' not in COMMON_PROBE_NAMES. "
                    f"Consider using a standard probe name."
                )

        if self.limit < 1:
            raise ValueError("limit must be at least 1")

        if self.offset < 0:
            raise ValueError("offset must be non-negative")


@dataclass
class SampleLinkRequest:
    """Request to link an artifact to a sample.

    Attributes
    ----------
    artifact_id : str
        The artifact ID to link.
    sample_id : str
        The sample ID to link to.

    """

    artifact_id: str
    sample_id: str

    def __post_init__(self) -> None:
        """Validate required fields."""
        if not self.artifact_id:
            raise ValueError("artifact_id is required")

        if not self.sample_id:
            raise ValueError("sample_id is required")


@dataclass
class SampleUnlinkRequest:
    """Request to unlink an artifact from a sample.

    Attributes
    ----------
    artifact_id : str
        The artifact ID to unlink.
    sample_id : str
        The sample ID to unlink from.

    """

    artifact_id: str
    sample_id: str

    def __post_init__(self) -> None:
        """Validate required fields."""
        if not self.artifact_id:
            raise ValueError("artifact_id is required")

        if not self.sample_id:
            raise ValueError("sample_id is required")


@dataclass
class SampleSearchRequest:
    """Request to search samples by PDBx/mmCIF/flrCIF vocabulary keys.

    This request allows searching samples using standardized vocabulary keys
    from PDBx, pdbihm, or flrCIF dictionaries.

    Attributes
    ----------
    vocabulary_field : str, optional
        The vocabulary field name to search (e.g., "entity_type", "ph").
    vocabulary_value : str, optional
        The value to match against the vocabulary field.
    use_pdbx : bool, optional
        If ``True``, validate against PDBx dictionary keys. Default is ``True``.
    use_flrcif : bool, optional
        If ``True``, validate against flrCIF dictionary keys. Default is ``True``.

    """

    vocabulary_field: Optional[str] = None
    vocabulary_value: Optional[str] = None
    use_pdbx: bool = True
    use_flrcif: bool = True

    def __post_init__(self) -> None:
        """Validate vocabulary field."""
        if self.vocabulary_field:
            valid_fields = set()
            
            if self.use_pdbx or self.use_flrcif:
                try:
                    from mmfdb.schema.pdbx_metadata import MmcifDictionary
                    dic = MmcifDictionary.load_bundled()
                    # Get all category.field keys from the dictionary
                    # Include both with and without leading underscore (e.g., "flr_sample.id" and "_flr_sample.id")
                    for category_name, category in dic._categories.items():
                        if hasattr(category, 'items') and category.items:
                            # Only include flr_* categories if use_flrcif, all categories if use_pdbx
                            if self.use_pdbx or (self.use_flrcif and category_name.startswith("flr_")):
                                for attr_name, item in category.items.items():
                                    # Add both forms: category.attribute and _category.attribute
                                    valid_fields.add(f"{category_name}.{attr_name}")
                                    valid_fields.add(f"_{category_name}.{attr_name}")
                                
                                # Also add using the full item name (which includes category prefix)
                                for attr_name, item in category.items.items():
                                    if item.name:
                                        valid_fields.add(item.name)
                except Exception:
                    pass  # Dictionary not available, skip validation
            
            # Check if the field is valid
            if valid_fields:
                # Allow partial matches for flexibility
                matching_fields = [f for f in valid_fields if self.vocabulary_field in f]
                if not matching_fields:
                    raise ValueError(
                        f"vocabulary_field '{self.vocabulary_field}' not found in vocabulary. "
                        f"Valid fields include: {', '.join(sorted(valid_fields)[:10])}..."
                    )
