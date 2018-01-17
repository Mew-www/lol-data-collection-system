class Tiers:
    __tiers_enum = list(enumerate([
        "BRONZE V",   "BRONZE IV",   "BRONZE III",   "BRONZE II",   "BRONZE I",
        "SILVER V",   "SILVER IV",   "SILVER III",   "SILVER II",   "SILVER I",
        "GOLD V",     "GOLD IV",     "GOLD III",     "GOLD II",     "GOLD I",
        "PLATINUM V", "PLATINUM IV", "PLATINUM III", "PLATINUM II", "PLATINUM I",
        "DIAMOND V",  "DIAMOND IV",  "DIAMOND III",  "DIAMOND II",  "DIAMOND I",
        "MASTER I",
        "CHALLENGER I"
    ]))

    def get_numeric_tier_repr(self, textual_tier):
        numeric_repr = next(filter(lambda enum: enum[1] == textual_tier, self.__tiers_enum), None)[0]
        if numeric_repr is None:
            raise ValueError('Unconfigured tier {}'.format(textual_tier))
        return numeric_repr

    def get_textual_tier_repr(self, numeric_tier):
        textual_repr = next(filter(lambda enum: enum[0] == numeric_tier, self.__tiers_enum), None)[1]
        if textual_repr is None:
            raise ValueError('Unconfigured tier {}'.format(numeric_tier))
        return textual_repr

    def get_average(self, tiers):
        ranked_tiers = filter(lambda t: t != "UNRANKED", tiers)
        numeric_tiers = list(map(lambda t: int(self.get_numeric_tier_repr(t)), ranked_tiers))
        average_numeric_tier = round(sum(numeric_tiers) / len(numeric_tiers))
        return self.get_textual_tier_repr(average_numeric_tier)
